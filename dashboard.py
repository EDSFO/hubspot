import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import date, datetime
from html import escape
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

DATA_FILE = Path("resumos_hubspot.json")
ENV_FILE = Path(".env")
load_dotenv()
TARGET_OWNER_NAME = os.getenv("HUBSPOT_OWNER_NAME", "Erick Douglas Sousa de Freitas Oliveira").strip()
TARGET_OWNER_NAMES_RAW = os.getenv("HUBSPOT_OWNER_NAMES", "").strip()
INCLUDED_STAGES_RAW = os.getenv("HUBSPOT_INCLUDED_STAGES", "Em diagnóstico,Em negociação").strip()
ALL_OWNERS_LABEL = "(Todos os responsaveis)"


def running_inside_docker() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("DOTNET_RUNNING_IN_CONTAINER", "").strip().lower() == "true"


def parse_brl_to_float(raw: str) -> float:
    if not raw:
        return 0.0
    text = raw.strip().replace("R$", "").replace(" ", "")
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_activity_date(raw: str):
    if not raw:
        return None
    match = re.search(r"(\d{2}/\d{2}/\d{4})", raw)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def risk_status(days_without_activity):
    if days_without_activity is None:
        return "Sem informacao"
    if days_without_activity > 14:
        return "Alto"
    if days_without_activity > 7:
        return "Medio"
    return "Baixo"


def format_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def shorten(text: str, limit: int = 180) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value or "--"
    return value[:limit].rstrip() + "..."


def repair_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    if any(token in value for token in ("Ã", "Â", "â€", "ðŸ", "??")):
        try:
            repaired = value.encode("latin1").decode("utf-8")
            if repaired.strip():
                value = repaired
        except Exception:
            pass
    return value.replace("\xa0", " ")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", repair_text(text or "")).strip()


def load_data(path: Path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_data_via_script(
    selected_stages: list[str] | None = None,
    selected_owners: list[str] | None = None,
    selected_risks: list[str] | None = None,
    timeout_sec: int = 1800,
) -> tuple[bool, str]:
    try:
        run_env = os.environ.copy()
        if selected_stages is not None:
            cleaned_stages = [s for s in selected_stages if s and not is_excluded_stage(s)]
            run_env["HUBSPOT_INCLUDED_STAGES"] = ",".join(cleaned_stages)
        if selected_owners is not None:
            owners_value = ",".join([o for o in selected_owners if o]).strip()
            run_env["HUBSPOT_OWNER_NAMES"] = owners_value if owners_value else "*"
        if selected_risks is not None:
            run_env["HUBSPOT_INCLUDED_RISKS"] = ",".join([r for r in selected_risks if r])
        result = subprocess.run(
            [sys.executable, "script.py"],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=run_env,
        )
    except subprocess.TimeoutExpired:
        return False, "Timeout ao executar script.py. Verifique login no HubSpot e conectividade."
    except Exception as exc:
        return False, f"Falha ao executar script.py: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        msg = stderr if stderr else stdout
        if "Sessao invalida no HubSpot em modo headless" in msg:
            refresh_hint = (
                "Sessao do HubSpot expirada no Docker. "
                "Renove o arquivo hubspot-session.json fora do container com "
                "`python script.py --refresh-session` e, depois, tente atualizar novamente."
            )
            return False, f"{refresh_hint}\n\nDetalhes:\n{msg[:1400]}"
        return False, f"script.py retornou erro (code {result.returncode}): {msg[:1400]}"

    output = (result.stdout or "Atualizacao concluida com sucesso.").strip()
    return True, output[-3000:]


def refresh_hubspot_session_via_script(timeout_sec: int = 1200) -> tuple[bool, str]:
    if running_inside_docker():
        return (
            False,
            "A renovacao interativa da sessao nao pode ser executada dentro do Docker. "
            "Rode `python script.py --refresh-session` no host, fora do container, para abrir a janela do HubSpot e salvar o novo hubspot-session.json.",
        )
    try:
        run_env = os.environ.copy()
        run_env["HUBSPOT_HEADLESS"] = "false"
        result = subprocess.run(
            [sys.executable, "script.py", "--refresh-session"],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=run_env,
        )
    except subprocess.TimeoutExpired:
        return False, "Timeout ao renovar a sessao. Conclua o login do HubSpot na janela aberta e tente novamente."
    except Exception as exc:
        return False, f"Falha ao executar a renovacao de sessao: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        msg = stderr if stderr else stdout
        return False, f"Falha ao renovar sessao (code {result.returncode}): {msg[:1400]}"

    output = (result.stdout or "Sessao renovada com sucesso.").strip()
    return True, output[-3000:]


def enrich(items: list[dict]):
    today = date.today()
    enriched = []
    for item in items:
        amount = parse_brl_to_float(item.get("valor", ""))
        activity_date = parse_activity_date(item.get("ultima_atividade", ""))
        days_without = (today - activity_date).days if activity_date else None
        inter_root = item.get("ultimas_interacoes") or {}
        inter, by_tab = sanitize_interaction_root(inter_root)
        canonical_stage = item.get("etapa") or "Sem etapa"
        norm_stage = normalize_owner_name(canonical_stage)
        for label in INCLUDED_STAGE_LABELS:
            if normalize_owner_name(label) == norm_stage:
                canonical_stage = label
                break
        company = item.get("empresa") or infer_company_from_name(item.get("nome", ""))
        enriched.append(
            {
                **item,
                "valor_num": amount,
                "dias_sem_atividade": days_without,
                "risco": risk_status(days_without),
                "etapa": canonical_stage,
                "proprietario": item.get("proprietario") or "Sem responsavel",
                "empresa": company or "Nao vinculada",
                "interacoes": inter,
                "interacoes_por_aba": by_tab,
            }
        )
    return enriched


def deal_quality_flags(deal: dict) -> list[str]:
    flags = []
    if not normalize_whitespace(deal.get("nome", "")):
        flags.append("negocio sem nome")
    if not normalize_whitespace(deal.get("empresa", "")):
        flags.append("empresa ausente")

    inter = deal.get("interacoes") or {}
    comparable = []
    for field in ("observacao", "email", "tarefa", "reuniao"):
        value = normalize_owner_name(inter.get(field, ""))
        if value:
            comparable.append(value)
    if len(comparable) >= 2 and len(set(comparable)) == 1:
        flags.append("interacoes duplicadas entre abas")
    return flags


def normalize_owner_name(value: str) -> str:
    text = (value or "").strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def classify_interaction_text(text: str) -> str:
    low = normalize_owner_name(text)
    if "tarefa" in low:
        return "tarefa"
    if "reuni" in low or "meeting" in low:
        return "reuniao"
    if "e mail" in low or "email" in low:
        return "email"
    if "observa" in low or "nota" in low:
        return "observacao"
    return "atividade"


def infer_company_from_name(name: str) -> str:
    text = normalize_whitespace(name)
    if not text:
        return ""
    parts = [normalize_whitespace(part) for part in re.findall(r"\[([^\]]+)\]", text) if normalize_whitespace(part)]
    if parts:
        return parts[0]
    if " - " in text:
        return normalize_whitespace(text.split(" - ", 1)[0])
    return ""


def sanitize_interaction_root(inter_root: dict | None) -> tuple[dict, dict]:
    tabs = ["atividade", "observacao", "email", "tarefa", "reuniao"]
    root = inter_root or {}
    raw_by_tab = root.get("por_aba", {}) if isinstance(root, dict) else {}
    by_tab = {tab: [] for tab in tabs}

    for tab in tabs:
        seen = set()
        for raw in raw_by_tab.get(tab) or []:
            text = normalize_whitespace(raw)
            key = normalize_owner_name(text)
            if not text or not key or key in seen:
                continue
            seen.add(key)
            by_tab[tab].append(text)

    latest = {tab: (by_tab[tab][0] if by_tab[tab] else "") for tab in tabs}
    if not latest["atividade"]:
        latest["atividade"] = next((text for text in by_tab["tarefa"] + by_tab["email"] + by_tab["reuniao"] + by_tab["observacao"] if text), "")
    return latest, by_tab


def parse_included_stages(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        norm = normalize_owner_name(part)
        tokens = norm.split()
        is_lost = any(t.startswith("perdid") for t in tokens) or ("closed" in tokens and "lost" in tokens)
        if norm:
            if is_lost:
                continue
            out.append(norm)
    return out


INCLUDED_STAGES = parse_included_stages(INCLUDED_STAGES_RAW)
INCLUDED_STAGE_LABELS = [s.strip() for s in INCLUDED_STAGES_RAW.split(",") if s.strip()]


def parse_owner_names(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        value = part.strip()
        if value:
            out.append(value)
    return out


DEFAULT_OWNERS = parse_owner_names(TARGET_OWNER_NAMES_RAW) or ([TARGET_OWNER_NAME] if TARGET_OWNER_NAME else [])


def stage_matches(norm_stage: str, norm_target: str) -> bool:
    if not norm_stage or not norm_target:
        return False
    if norm_target in norm_stage:
        return True
    stage_tokens = [t for t in norm_stage.split() if len(t) >= 3]
    target_tokens = [t for t in norm_target.split() if len(t) >= 3]
    if not target_tokens:
        return False
    for tok in target_tokens:
        key = tok[:4]
        if not any(s.startswith(key) for s in stage_tokens):
            return False
    return True


def is_included_stage(stage_name: str) -> bool:
    if not INCLUDED_STAGES:
        return True
    norm = normalize_owner_name(stage_name)
    if not norm:
        return False
    return any(stage_matches(norm, target) for target in INCLUDED_STAGES)


def is_target_owner(owner_name: str) -> bool:
    if not TARGET_OWNER_NAME:
        return True
    return normalize_owner_name(owner_name) == normalize_owner_name(TARGET_OWNER_NAME)


def is_excluded_stage(stage_name: str) -> bool:
    norm = normalize_owner_name(stage_name)
    tokens = norm.split()
    has_neg = any(t.startswith("neg") for t in tokens)
    has_closed_lost = "closed" in tokens and "lost" in tokens
    has_lost = any(t.startswith("perdid") for t in tokens) or has_closed_lost
    return (has_neg and has_lost) or has_closed_lost


def get_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value

    # Fallback parser: aceita chaves legadas com espacos/hifens no .env.
    if ENV_FILE.exists():
        try:
            normalized_targets = {re.sub(r"[^a-z0-9]", "", k.lower()) for k in keys}
            for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                content = line.strip()
                if not content or content.startswith("#") or "=" not in content:
                    continue
                k, v = content.split("=", 1)
                nk = re.sub(r"[^a-z0-9]", "", k.strip().lower())
                if nk in normalized_targets:
                    val = v.strip().strip("\"' ")
                    if val:
                        return val
        except Exception:
            pass
    return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = get_env(name, default="").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "sim"}


def format_env_value(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if any(ch.isspace() for ch in text) or "#" in text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f"\"{escaped}\""
    return text


def upsert_env_value(key: str, value: str) -> None:
    clean_key = (key or "").strip()
    if not clean_key:
        raise ValueError("Chave de ambiente invalida.")

    rendered = f"{clean_key}={format_env_value(value)}"
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()

    replaced = False
    for idx, line in enumerate(lines):
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        current_key = raw.split("=", 1)[0].strip()
        if current_key == clean_key:
            lines[idx] = rendered
            replaced = True
            break

    if not replaced:
        lines.append(rendered)

    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.environ[clean_key] = (value or "").strip()


def build_local_executive_summary(filtered: list[dict]) -> str:
    if not filtered:
        return "Resumo Executivo de Negocios\nSem negocios para os filtros selecionados."

    lines = ["Resumo Executivo de Negocios — Pipeline AIOS", ""]
    ordered = sorted(filtered, key=lambda d: d.get("valor_num", 0.0), reverse=True)
    for idx, d in enumerate(ordered[:8], start=1):
        evidence = collect_all_relevant_evidence(d)
        contexto = build_account_update_text(d, evidence)

        lines.append(f"{idx}) {d.get('nome', 'Sem nome')}")
        lines.append(f"Segmento: {d.get('empresa', 'Nao vinculado')}")
        lines.append(f"Status: {d.get('etapa', 'Sem etapa')}")
        lines.append(f"Valor: {d.get('valor', '--')}")
        lines.append("Contexto da demanda")
        lines.append(f"- {shorten(contexto, 320)}")
        lines.append("Situacao atual")
        lines.append(f"- Risco atual: {d.get('risco', 'Sem informacao')}")
        lines.append("Leitura comercial")
        lines.append("- Interesse existente")
        lines.append("- Necessario alinhamento entre areas e decisores")
        lines.append("Proximo passo")
        lines.append("- Manter follow-up ativo e confirmar proximo marco do processo de compra")
        lines.append("")
    return "\n".join(lines).strip()


def latest_interaction_text(deal: dict, interaction_type: str) -> str:
    by_tab = deal.get("interacoes_por_aba") or {}
    values = by_tab.get(interaction_type, [])
    if isinstance(values, list):
        for item in values:
            if isinstance(item, str) and item.strip():
                return item.strip()

    inter = deal.get("interacoes") or {}
    fallback = inter.get(interaction_type, "")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return "(sem atualizacao recente)"


def parse_deal_title(raw_name: str) -> tuple[str, str]:
    name = normalize_whitespace(raw_name) or "Sem nome"
    parts = [normalize_whitespace(part) for part in re.findall(r"\[([^\]]+)\]", name) if normalize_whitespace(part)]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], parts[0]
    if " - " in name:
        left, right = name.split(" - ", 1)
        return normalize_whitespace(left), normalize_whitespace(right)
    return name, name


def summarize_interaction_text(raw_text: str, limit: int = 260) -> str:
    text = normalize_whitespace(raw_text)
    if not text:
        return ""

    # Remove prefixos comuns de conversas exportadas (ex.: WhatsApp).
    text = re.sub(r"\[\d{1,2}:\d{2},\s*\d{2}/\d{2}/\d{4}\]\s*[^:]{1,60}:\s*", " ", text)

    markers = [
        "Corpo do e-mail",
        "Descricao do participante",
        "Descrição do participante",
        "Resumo da Reuniao",
        "Resumo da Reunião",
    ]
    for marker in markers:
        if marker in text:
            text = text.split(marker, 1)[-1].strip(" :-")

    boilerplate_patterns = [
        r"https?://\S+",
        r"\bPressione para copiar um link.*?$",
        r"\bResponder a todos\b",
        r"\bResponder\b",
        r"\bEncaminhar\b",
        r"\bEnviados\b",
        r"\bSaiba mais\b",
        r"\bExibir thread\b.*$",
        r"\bExpandir\b",
        r"\bOs dados de rastreamento.*?$",
        r"\bIngressar:.*$",
        r"\bID da Reuni[aã]o:.*$",
        r"\bSenha:.*$",
        r"\bAdicionar descri[cç][aã]o\b",
        r"\bParticipar da videoconfer[eê]ncia\b",
        r"\bResumo de propriedades de Reuni[aã]o\b",
        r"\bTipo de\s*-\s*Resultado da\s*-\s*Hora de in[ií]cio da reuni[aã]o\b",
        r"\bParticipantes\s+\d+\s+participantes\b",
        r"\bDura[cç][aã]o\s+[^\.]+",
        r"\bTipo de localiza[cç][aã]o\s+[^\.]+",
        r"\[cid:[^\]]+\]",
        r"_+",
    ]
    for pattern in boilerplate_patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)

    text = re.sub(r"\bE-mail\s*-\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bReuni[aã]o\s*-\s*", "", text, flags=re.IGNORECASE)
    relation_prefixes = [
        r"^Com rela[cç][aã]o \xE0 observa[cç][aã]o registrada[:\s-]*",
        r"^Com relacao a observacao registrada[:\s-]*",
        r"^Com rela[cç][aã]o ao email[:\s-]*",
        r"^Com relacao ao email[:\s-]*",
        r"^Com rela[cç][aã]o \xE0 reuni[aã]o[:\s-]*",
        r"^Com relacao a reuniao[:\s-]*",
        r"^Com rela[cç][aã]o \xE0 tarefa[:\s-]*",
        r"^Com relacao a tarefa[:\s-]*",
    ]
    for pattern in relation_prefixes:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(bom dia|boa tarde|boa noite|ola|ol[aá]|opa|tudo bem)[!,.:\s-]+", "", text, flags=re.IGNORECASE)
    text = normalize_whitespace(text).strip(" .:-")
    if not text:
        return ""
    if re.fullmatch(r"https?://\S+", text, flags=re.IGNORECASE):
        return ""
    return shorten(text, limit)


def collect_recent_evidence(deal: dict, per_type: int = 2) -> list[str]:
    by_tab = deal.get("interacoes_por_aba") or {}
    evidence = []
    seen = set()
    for tab in ("observacao", "email", "reuniao", "tarefa", "atividade"):
        items = by_tab.get(tab) or []
        for raw_text in items[:per_type]:
            text = summarize_interaction_text(raw_text, limit=600)
            key = normalize_owner_name(text)
            if text and key and key not in seen:
                evidence.append(text)
                seen.add(key)
    return evidence


def collect_all_relevant_evidence(
    deal: dict,
    per_tab_limits: dict[str, int] | None = None,
) -> list[str]:
    by_tab = deal.get("interacoes_por_aba") or {}
    limits = per_tab_limits or {
        "observacao": 6,
        "email": 3,
        "reuniao": 3,
        "tarefa": 4,
        "atividade": 8,
    }
    evidence = []
    seen = set()
    for tab in ("observacao", "email", "reuniao", "tarefa", "atividade"):
        items = by_tab.get(tab) or []
        for raw_text in items[: limits.get(tab, 4)]:
            text = summarize_interaction_text(raw_text, limit=900)
            key = normalize_owner_name(text)
            if not text or not key or key in seen:
                continue
            evidence.append(text)
            seen.add(key)
    return evidence


SIGNAL_KEYWORDS = (
    "proposta",
    "contrato",
    "assinatura",
    "pagamento",
    "faturamento",
    "orcamento",
    "budget",
    "compras",
    "rfp",
    "prazo",
    "data",
    "retorno",
    "kickoff",
    "site survey",
    "implant",
    "cronograma",
    "validacao",
    "piloto",
    "teste",
    "decisor",
    "aprovacao",
    "pendencia",
    "bloqueio",
    "trava",
    "risco",
    "concorrente",
    "ferias",
)


def score_signal_text(text: str) -> int:
    norm = normalize_owner_name(text)
    if not norm:
        return -10

    score = 0
    score += sum(2 for keyword in SIGNAL_KEYWORDS if keyword in norm)

    if re.search(r"\b\d{2}/\d{2}/\d{4}\b", text):
        score += 2
    if re.search(r"\bR\$\s?\d", text):
        score += 2
    if re.search(r"\b\d+\s*(dias?|semanas?|meses?)\b", norm):
        score += 1

    token_count = len(norm.split())
    if token_count >= 18:
        score += 1
    if token_count <= 6:
        score -= 2

    low_signal_patterns = (
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
        "sem novidade",
        "sem atualizacao",
        "sem atualização",
        "ok",
        "blz",
    )
    if any(pattern in norm for pattern in low_signal_patterns):
        score -= 2

    return score


def select_informative_observations(deal: dict, limit: int = 3, pool_size: int = 8) -> list[str]:
    by_tab = deal.get("interacoes_por_aba") or {}
    observations = []
    seen = set()
    candidates = []
    for idx, raw_text in enumerate((by_tab.get("observacao") or [])[:pool_size]):
        text = summarize_interaction_text(raw_text, limit=650)
        key = normalize_owner_name(text)
        if text and key and key not in seen:
            seen.add(key)
            candidates.append((score_signal_text(text), -idx, text))

    candidates.sort(reverse=True)
    observations = [text for score, _, text in candidates if score >= -1][:limit]

    if len(observations) < limit:
        ordered = sorted(candidates, key=lambda item: item[1], reverse=True)
        for _, _, text in ordered:
            if text not in observations:
                observations.append(text)
            if len(observations) >= limit:
                break
    return observations


def collect_recent_observations(deal: dict, limit: int = 3) -> list[str]:
    return select_informative_observations(deal, limit=limit, pool_size=max(6, limit * 3))


def extract_fact_candidates(text: str) -> list[tuple[int, str]]:
    cleaned = summarize_interaction_text(text, limit=1400)
    if not cleaned:
        return []

    chunks = re.split(r"(?<=[\.\!\?;])\s*|(?<=\))\s+|(?<=:)\s+|\s{2,}", cleaned)
    out = []
    for raw_chunk in chunks:
        chunk = normalize_whitespace(raw_chunk).strip(" -")
        if not chunk:
            continue
        if len(chunk.split()) < 5:
            continue
        score = score_signal_text(chunk)
        if score < 1:
            continue
        out.append((score, shorten(chunk, 220)))
    return out


def build_key_fact_bullets(deal: dict, max_bullets: int = 4) -> list[str]:
    raw_sources = collect_all_relevant_evidence(deal)

    scored = []
    seen = set()
    for raw_text in raw_sources:
        for score, snippet in extract_fact_candidates(raw_text):
            key = normalize_owner_name(snippet)
            if key and key not in seen:
                seen.add(key)
                scored.append((score, snippet))

    scored.sort(key=lambda item: item[0], reverse=True)
    facts = [snippet for _, snippet in scored[:max_bullets]]

    if facts:
        return facts

    fallback_obs = collect_recent_observations(deal, limit=min(2, max_bullets))
    if fallback_obs:
        return [shorten(obs, 220) for obs in fallback_obs]
    return ["(sem fatos objetivos recentes nas interacoes)"]


def preferred_summary_backend_label() -> str:
    openrouter_key = get_env("OPENROUTER_API_KEY")
    if openrouter_key:
        model = get_env("OPENROUTER_MODEL", default="openai/gpt-4o-mini-2024-07-18")
        return f"OpenRouter ({model})"
    model = get_env("MINIMAX_MODEL", "Minimax_MODEL", default="MiniMax-M2.5")
    return f"MiniMax ({model})"


def detect_evidence_signals(evidence: list[str]) -> dict[str, bool]:
    combined = normalize_owner_name(" ".join(evidence))
    return {
        "proposal": any(keyword in combined for keyword in ("proposta", "contrato", "assinatura", "condicoes comerciais", "modelo de pagamento")),
        "implementation": any(keyword in combined for keyword in ("kickoff", "implant", "cronograma", "site survey", "setup", "calibr")),
        "validation": any(keyword in combined for keyword in ("poc", "teste", "piloto", "validacao", "validação")),
        "waiting": any(keyword in combined for keyword in ("aguardo", "retorno", "sem prazo", "sem data", "novidade")),
        "internal_alignment": any(keyword in combined for keyword in ("pressao interna", "pressão interna", "multiplas areas", "múltiplas áreas", "fornecedores", "decisores", "alinhamento")),
        "positive_feedback": any(keyword in combined for keyword in ("positivo", "cumpriu", "muito positivo", "boa percepcao", "boa percepção")),
    }


def infer_product_label(deal: dict, evidence: list[str]) -> str:
    raw_interactions = json.dumps(deal.get("interacoes_por_aba", {}), ensure_ascii=False)
    combined = normalize_owner_name(" ".join([deal.get("nome", ""), raw_interactions, *evidence]))
    if "aibox" in combined and "edge" in combined:
        return "AIOS Edge + AIBox"
    if "aibox" in combined:
        return "AIOS + AIBox"
    if "edge" in combined:
        return "AIOS Edge"
    return "AIOS"


def build_context_text(deal: dict, evidence: list[str], opportunity_name: str) -> str:
    signals = detect_evidence_signals(evidence)
    product = infer_product_label(deal, evidence)
    if signals["validation"]:
        return f"Oportunidade relacionada a validacao tecnica ou piloto da solucao {product} para {opportunity_name.lower()}."
    if signals["implementation"]:
        return f"Projeto em discussao para implantacao da solucao {product}, com necessidade de alinhamento operacional."
    if signals["proposal"]:
        return f"Demanda comercial em torno da proposta e da formalizacao da solucao {product} para {opportunity_name.lower()}."
    return f"Demanda relacionada a {opportunity_name.lower()} com uso potencial da solucao {product}."


def sentence_case(text: str) -> str:
    value = normalize_whitespace(text)
    if not value:
        return ""
    return value[:1].upper() + value[1:]


def ensure_period(text: str) -> str:
    value = normalize_whitespace(text)
    if not value:
        return ""
    if value[-1] in ".!?":
        return value
    return value + "."


def first_matching_evidence(evidence: list[str], *patterns: str) -> str:
    for text in evidence:
        norm = normalize_owner_name(text)
        if any(pattern in norm for pattern in patterns):
            return text
    return ""


def extract_slashed_date_time(text: str) -> str:
    match = re.search(r"(\d{2}/\d{2}(?:/\d{4})?(?:\s+as\s+\d{1,2}:\d{2})?)", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def account_specific_summary(deal: dict, evidence: list[str]) -> tuple[str, str]:
    combined = normalize_owner_name(" ".join(evidence))
    value = normalize_whitespace(deal.get("valor", ""))

    if any(token in combined for token in ("divisao de receita", "produto nosso", "estruturar como produto")):
        memo = (
            "O principal ponto de decisao levantado internamente e definir o modelo de negocio: "
            "parceria com divisao de receita ou produto proprio para escalar. "
            "Sem essa definicao, a oportunidade segue travada."
        )
        next_step = "Aguardando Leonardo Moreno definir o modelo de negocio para destravar o proximo passo."
        return memo, next_step

    if any(token in combined for token in ("nao recebi nenhuma mensagem", "ninguem entrou em contato", "o pessoal ficou de entrar em contato", "me mandar o e mail", "deixar o numero")):
        memo = (
            "O avanco com a Patrus depende do contato de um apoiador interno da Sonda, "
            "mas esse encaminhamento ainda nao aconteceu. "
            "Sem esse ponto de entrada, a negociacao fica parada."
        )
        next_step = "Erick vai localizar o apoiador na Sonda e obter o contato direto da Patrus para retomar a negociacao."
        return memo, next_step

    meeting_text = first_matching_evidence(evidence, "vamos nos reunir com o cliente", "reunir com o cliente")
    if meeting_text:
        date_txt = extract_slashed_date_time(meeting_text)
        if date_txt:
            memo = (
                f"Reuniao de diagnostico agendada para {date_txt} com o cliente, "
                "com foco em entender a dor e detalhar o escopo da demanda."
            )
            next_step = f"Erick vai conduzir a reuniao marcada para {date_txt} e coletar os dados para a proxima proposta."
        else:
            memo = sentence_case(shorten(meeting_text, 220))
            next_step = "Erick vai conduzir a proxima reuniao com o cliente e transformar o diagnostico em proposta."
        return memo, next_step

    if "nota fiscal" in combined or re.search(r"\bnf\b", combined):
        memo = "Negocio fechado, com faturamento e tratativas de nota fiscal em andamento."
        if "larissa" in combined:
            next_step = "Erick esta monitorando o faturamento com a Larissa para fechar as pendencias operacionais."
        else:
            next_step = "Erick vai acompanhar o faturamento e garantir o fechamento das pendencias de nota fiscal."
        return memo, next_step

    if any(token in combined for token in ("fabrizio", "petronilo", "proposta faseada", "verde brasil")) and value:
        memo = (
            f"Proposta de {value} em revalidacao antes de novo envio aos decisores do cliente. "
            "A conta segue em negociacao ativa e depende desse ajuste para avancar."
        )
        next_step = "Aguardando a validacao final do escopo para reenviar a proposta e retomar a negociacao."
        return memo, next_step

    if "manter essa proposta" in combined or "validar se podemos manter essa proposta" in combined:
        memo = "Foi feita uma consulta interna para validar se a proposta atual ainda se sustenta nas condicoes combinadas."
        next_step = "Aguardando confirmacao se a proposta pode ser mantida ou se precisa ser revalidada."
        return memo, next_step

    if "whatsapp" in combined and "reativar as negociacoes" in combined:
        memo = "Foi enviado um contato de retomada por WhatsApp para recolocar os projetos na pauta e reabrir a conversa comercial."
        next_step = "Erick vai reforcar a retomada por e-mail e tentar agendar uma nova reuniao de diagnostico."
        return memo, next_step

    return "", ""


def build_account_update_text(deal: dict, evidence: list[str]) -> str:
    custom_memo, _ = account_specific_summary(deal, evidence)
    if custom_memo:
        return custom_memo

    status = normalize_whitespace(deal.get("etapa", "Sem etapa")) or "Sem etapa"
    status_phrase = status.lower()
    last_activity = normalize_whitespace(deal.get("ultima_atividade", ""))
    risk = deal.get("risco", "")
    signals = detect_evidence_signals(evidence)
    parts = []

    if signals["proposal"] and signals["implementation"]:
        parts.append("A conta esta entre ajuste comercial da proposta e preparacao do proximo marco operacional.")
    elif signals["proposal"]:
        parts.append("A conta segue em negociacao comercial, com dependencia de validacao para formalizacao.")
    elif signals["validation"] and signals["positive_feedback"]:
        parts.append("A conta avancou com boa receptividade na validacao tecnica e pode evoluir para proposta.")
    elif signals["validation"]:
        parts.append("A conta ainda depende de validacao tecnica e consolidacao do escopo antes do proximo passo comercial.")
    elif signals["waiting"]:
        parts.append("A conta esta em compasso de espera, dependente de retorno do cliente ou confirmacao de agenda.")
    elif signals["internal_alignment"]:
        parts.append("A conta depende de alinhamento interno entre envolvidos para destravar a avancada comercial.")
    else:
        if status_phrase.startswith("em "):
            parts.append(f"A conta permanece {status_phrase}, sem evidencia suficiente de mudanca estrutural no momento.")
        else:
            parts.append(f"A conta permanece em {status_phrase}, sem evidencia suficiente de mudanca estrutural no momento.")

    if risk == "Alto":
        parts.append("O risco esta alto por falta de tracao recente e pede retomada ativa do follow-up.")
    elif risk == "Medio":
        parts.append("O risco esta medio e pede confirmacao de prazo e proximo marco.")

    if last_activity:
        parts.append(f"A ultima atividade registrada foi em {last_activity}.")
    else:
        parts.append("Nao ha data recente de atividade registrada.")

    return " ".join(parts)


def build_current_status_text(deal: dict, evidence: list[str]) -> str:
    status = normalize_whitespace(deal.get("etapa", "Sem etapa")) or "Sem etapa"
    last_activity = normalize_whitespace(deal.get("ultima_atividade", "")) or "sem data recente"
    signals = detect_evidence_signals(evidence)

    if not evidence:
        summary = "Sem evidencia recente suficiente para detalhar o momento da conta."
    elif signals["proposal"] and signals["implementation"]:
        summary = "As interacoes recentes indicam que o cliente esta entre a validacao comercial e a preparacao para execucao."
    elif signals["proposal"]:
        summary = "As interacoes recentes mostram discussao comercial ativa e dependencia de confirmacao para formalizacao."
    elif signals["validation"] and signals["positive_feedback"]:
        summary = "As interacoes recentes apontam boa receptividade do cliente apos validacao tecnica."
    elif signals["validation"]:
        summary = "As interacoes recentes indicam andamento de validacao tecnica antes do proximo passo comercial."
    elif signals["waiting"]:
        summary = "As interacoes recentes mostram dependencia de retorno do cliente para destravar o avanco."
    elif signals["internal_alignment"]:
        summary = "As interacoes recentes sugerem travas internas e necessidade de alinhamento entre envolvidos."
    else:
        summary = "As interacoes recentes confirmam andamento do negocio, mas sem gatilho claro de decisao imediata."

    return f"{summary} Status atual no pipeline: {status}. Ultima atividade registrada em {last_activity}."


def build_commercial_reading_bullets(deal: dict, evidence: list[str]) -> list[str]:
    stage = normalize_owner_name(deal.get("etapa", ""))
    combined = normalize_owner_name(" ".join(evidence))
    bullets = []

    if "negocia" in stage:
        bullets.append("Negocio em negociacao, exigindo conversao rapida para decisao formal.")
    elif "diagnost" in stage:
        bullets.append("Oportunidade ainda em diagnostico, com necessidade de consolidar escopo e patrocinio interno.")
    elif "fechado" in stage:
        bullets.append("Negocio em fase avancada de fechamento ou formalizacao.")

    keyword_bullets = [
        (("proposta", "contrato", "assinatura", "condicoes comerciais", "modelo de pagamento"), "Ha evidencia comercial concreta de proposta ou condicoes de contratacao em circulacao."),
        (("kickoff", "implant", "cronograma", "site survey", "setup", "calibr"), "As interacoes ja apontam para alinhamento operacional ou proximo marco de implantacao."),
        (("poc", "teste", "piloto", "validacao"), "Existe sinal de validacao tecnica ou piloto como gatilho para avancar o negocio."),
        (("aguardo", "retorno", "sem prazo", "sem data", "novidade"), "O avanco depende de retorno do cliente ou de uma data de decisao mais clara."),
        (("pressao interna", "multiplas areas", "múltiplas áreas", "fornecedores", "decisores", "alinhamento"), "Ha dependencia de alinhamento interno entre areas, decisores ou fornecedores."),
        (("positivo", "cumpriu", "muito positivo", "boa percepcao", "boa percepção"), "O tom recente sugere percepcao favoravel do cliente sobre a solucao."),
    ]
    for keywords, bullet in keyword_bullets:
        if any(keyword in combined for keyword in keywords):
            bullets.append(bullet)

    risk = deal.get("risco", "")
    if risk == "Alto":
        bullets.append("Risco alto por falta de atividade recente, pedindo retomada ativa do follow-up.")
    elif risk == "Medio":
        bullets.append("Risco medio de perda de timing, recomendando acompanhamento com data fechada.")

    deduped = []
    seen = set()
    for bullet in bullets:
        key = normalize_owner_name(bullet)
        if key and key not in seen:
            deduped.append(bullet)
            seen.add(key)
        if len(deduped) >= 3:
            break

    return deduped or ["Leitura comercial sem evidencia suficiente nas ultimas interacoes."]


def build_next_step_bullets(deal: dict, evidence: list[str]) -> list[str]:
    _, custom_next_step = account_specific_summary(deal, evidence)
    if custom_next_step:
        return [custom_next_step]

    stage = normalize_owner_name(deal.get("etapa", ""))
    combined = normalize_owner_name(" ".join(evidence))
    bullets = []

    if any(keyword in combined for keyword in ("proposta", "contrato", "assinatura", "condicoes comerciais", "modelo de pagamento")):
        bullets.append("Vou cobrar a validacao final da proposta e confirmar a data de formalizacao.")
    if any(keyword in combined for keyword in ("kickoff", "implant", "cronograma", "site survey", "setup", "calibr")):
        bullets.append("Vou alinhar cronograma, responsaveis e marco tecnico da implantacao.")
    if any(keyword in combined for keyword in ("poc", "teste", "piloto", "validacao")):
        bullets.append("Vou definir o proximo marco tecnico da POC/piloto e o criterio de aprovacao.")
    if any(keyword in combined for keyword in ("aguardo", "retorno", "sem prazo", "sem data", "novidade")) or deal.get("risco") == "Alto":
        bullets.append("Vou retomar o contato com data objetiva de retorno e identificar os decisores ativos.")

    if not bullets:
        if "negocia" in stage:
            bullets.append("Vou conduzir o follow-up comercial para fechar pendencias e avancar para formalizacao.")
        elif "diagnost" in stage:
            bullets.append("Vou validar escopo, dor prioritaria e proximo checkpoint com cliente e time tecnico.")
        else:
            bullets.append("Vou confirmar o proximo marco do processo e os responsaveis pela decisao.")

    deduped = []
    seen = set()
    for bullet in bullets:
        key = normalize_owner_name(bullet)
        if key and key not in seen:
            deduped.append(bullet)
            seen.add(key)
        if len(deduped) >= 2:
            break

    return deduped


def split_sentences(text: str) -> list[str]:
    value = normalize_whitespace(text)
    if not value:
        return []
    parts = re.split(r"(?<=[\.\!\?])\s+", value)
    return [normalize_whitespace(part).rstrip(".") for part in parts if normalize_whitespace(part)]


def combined_deal_context_text(deal: dict, evidence: list[str]) -> str:
    return normalize_owner_name(
        " ".join(
            [
                deal.get("nome", ""),
                deal.get("empresa", ""),
                deal.get("etapa", ""),
                deal.get("valor", ""),
                deal.get("ultima_atividade", ""),
                *evidence,
            ]
        )
    )


def infer_focus_points(deal: dict, evidence: list[str]) -> list[str]:
    combined = combined_deal_context_text(deal, evidence)
    points = []

    if any(token in combined for token in ("passagem de nivel", "locomotiva", "ferrovia", "ferrovi")):
        points.extend(
            [
                "Projeto ligado a monitoramento de passagem de nivel com foco em seguranca ferroviaria.",
                "A conversa sugere uso de visao computacional para correlacionar fluxo na area com eventos operacionais da linha.",
            ]
        )
    elif any(token in combined for token in ("epi", "equipamento de protecao", "equipamento de proteção")):
        points.extend(
            [
                "Oportunidade ligada a deteccao de EPI e reforco de conformidade em ambiente operacional.",
                "A proposta conversa com seguranca do trabalho e governanca de comportamento em campo.",
            ]
        )
    elif any(token in combined for token in ("qualidade", "inspecao", "inspeção", "falha", "falcoa")):
        points.extend(
            [
                "Oportunidade ligada a qualidade ou inspecao visual em processo operacional.",
                "O valor da conta depende de transformar controle visual em padrao recorrente de operacao.",
            ]
        )
    elif any(token in combined for token in ("produtividade", "eficiencia", "eficiência", "patrus")):
        points.extend(
            [
                "Conta conectada a produtividade operacional e ganho de eficiencia no processo monitorado.",
                "A tese comercial depende de provar impacto pratico e repetivel no dia a dia da operacao.",
            ]
        )
    else:
        opportunity_name = parse_deal_title(deal.get("nome", "Sem nome"))[1]
        points.append(f"Oportunidade ligada a {opportunity_name.lower()} com uso potencial de visao computacional aplicada ao processo do cliente.")

    stage = normalize_whitespace(deal.get("etapa", ""))
    if stage:
        points.append(f"A conta esta atualmente em {stage.lower()} no pipeline.")

    deduped = []
    seen = set()
    for point in points:
        key = normalize_owner_name(point)
        if key and key not in seen:
            deduped.append(point)
            seen.add(key)
        if len(deduped) >= 3:
            break
    return deduped


def build_value_potential_points(deal: dict, evidence: list[str]) -> list[str]:
    combined = combined_deal_context_text(deal, evidence)
    signals = detect_evidence_signals(evidence)
    points = []

    if deal.get("valor_num", 0.0) > 0:
        points.append(f"Valor monitorado hoje em pipeline: {format_brl(deal.get('valor_num', 0.0))}.")
    else:
        points.append("Valor ainda nao consolidado no pipeline, indicando conta em fase exploratoria, diagnostico ou validacao.")

    if any(token in combined for token in ("passagem de nivel", "ferrovia", "locomotiva", "epi", "seguranca")):
        points.append("Existe potencial de retorno por reducao de risco operacional e fortalecimento de seguranca no processo.")
    elif any(token in combined for token in ("qualidade", "inspecao", "inspeção")):
        points.append("O ganho potencial esta em padronizacao de qualidade, reducao de falhas e escalabilidade do controle visual.")
    elif any(token in combined for token in ("produtividade", "eficiencia", "eficiência")):
        points.append("O valor potencial esta ligado a produtividade, fluidez operacional e melhor uso da rotina em campo.")
    else:
        points.append("A oportunidade pode abrir espaco para expansao futura se a conta provar valor pratico em uma primeira entrega.")

    if signals["validation"] or signals["implementation"]:
        points.append("Se a validacao atual avancar, a conta pode virar caso de expansao para outras frentes ou unidades.")
    elif signals["proposal"]:
        points.append("Existe espaco para captura de valor se a proposta for validada e convertida em compromisso comercial.")

    return points[:3]


def build_current_situation_points(deal: dict, evidence: list[str]) -> list[str]:
    combined = combined_deal_context_text(deal, evidence)
    points = []

    for sentence in split_sentences(build_account_update_text(deal, evidence))[:2]:
        if sentence:
            points.append(ensure_period(sentence))

    if any(token in combined for token in ("nao esquecemos", "não esquecemos", "outra area", "outras demandas", "mais focados", "stand by", "standby")):
        points.append("A interacao recente indica que a necessidade existe, mas a prioridade interna do cliente esta em outro foco no momento.")
    elif any(token in combined for token in ("aguardo", "retorno", "sem prazo", "sem data")):
        points.append("O andamento imediato depende de retorno do cliente ou de definicao mais clara de data e responsavel.")
    elif any(token in combined for token in ("proposta", "contrato", "assinatura")):
        points.append("O momento atual da conta gira em torno de validacao comercial e destravamento para formalizacao.")

    days = deal.get("dias_sem_atividade")
    if days is not None:
        if days > 30:
            points.append(f"A conta acumula {days} dias sem atividade recente, sinalizando perda de tracao.")
        elif days > 7:
            points.append(f"A conta esta ha {days} dias sem atividade, o que pede acompanhamento proximo.")

    deduped = []
    seen = set()
    for point in points:
        key = normalize_owner_name(point)
        if key and key not in seen:
            deduped.append(point)
            seen.add(key)
        if len(deduped) >= 3:
            break
    return deduped or ["Sem evidencia suficiente para detalhar a situacao atual."]


def build_risk_commercial_points(deal: dict, evidence: list[str]) -> list[str]:
    combined = combined_deal_context_text(deal, evidence)
    risk = deal.get("risco", "")
    days = deal.get("dias_sem_atividade")
    points = []

    if risk == "Alto":
        points.append("Ha risco alto de alongamento de ciclo ou congelamento da conta sem gatilho claro de retomada.")
    elif risk == "Medio":
        points.append("Ha risco medio de perda de timing, com necessidade de follow-up orientado a prazo.")
    else:
        points.append("O risco comercial atual parece controlado, mas ainda depende de confirmacao do proximo marco.")

    if any(token in combined for token in ("nao esquecemos", "não esquecemos", "outra area", "outras demandas", "stand by", "standby")):
        points.append("A prioridade concorrente do cliente aumenta a chance de o negocio virar backlog em vez de pipeline ativo.")
    if any(token in combined for token in ("aguardo", "retorno", "sem prazo", "sem data")):
        points.append("Sem data ou dono claramente assumido, a conta tende a consumir energia comercial sem conversao proporcional.")
    if days is not None and days > 21:
        points.append("A janela longa sem atividade recente reduz urgencia e exige requalificacao do interesse real.")

    deduped = []
    seen = set()
    for point in points:
        key = normalize_owner_name(point)
        if key and key not in seen:
            deduped.append(point)
            seen.add(key)
        if len(deduped) >= 3:
            break
    return deduped


def build_bant_points(deal: dict, evidence: list[str]) -> list[str]:
    combined = combined_deal_context_text(deal, evidence)
    risk = deal.get("risco", "")

    if deal.get("valor_num", 0.0) > 0 or any(token in combined for token in ("proposta", "pagamento", "orcamento", "orçamento", "contrato")):
        budget = "ha indicio de verba ou escopo comercial em discussao, mas a confirmacao final ainda depende de validacao."
    else:
        budget = "nao validado."

    if any(token in combined for token in ("aprovacao", "aprovação", "compras", "diretor", "gerente", "leonardo", "larissa", "bruno")):
        authority = "existem interlocutores relevantes mapeados, mas a decisao final ainda nao esta totalmente clara."
    else:
        authority = "nao clara."

    if any(token in combined for token in ("seguranca", "segurança", "qualidade", "produtividade", "epi", "passagem de nivel", "validacao", "piloto")):
        if risk == "Alto" or any(token in combined for token in ("nao esquecemos", "não esquecemos", "outra area", "stand by", "standby")):
            need = "existe, mas nao virou prioridade imediata."
        else:
            need = "existe e conversa com uma dor operacional real."
    else:
        need = "ainda precisa ser melhor comprovado no contexto do cliente."

    if any(token in combined for token in ("13 de abr", "14 de abr", "reuniao", "reunião", "cronograma", "kickoff", "quinta feira")):
        timing = "ha marco ou checkpoint recente, mas ainda sem previsibilidade total de decisao."
    elif risk == "Alto" or any(token in combined for token in ("sem prazo", "sem data", "aguardo", "retorno")):
        timing = "indefinido."
    else:
        timing = "em acompanhamento, ainda sem data firme de fechamento."

    return [
        f"Budget: {budget}",
        f"Authority: {authority}",
        f"Need: {need}",
        f"Timing: {timing}",
    ]


def build_strategic_reading_text(deal: dict, evidence: list[str]) -> str:
    combined = combined_deal_context_text(deal, evidence)
    risk = deal.get("risco", "")
    stage = normalize_whitespace(deal.get("etapa", "Sem etapa")).lower()

    if any(token in combined for token in ("nao esquecemos", "não esquecemos", "outra area", "outras demandas", "stand by", "standby")):
        return "Nao parece perda declarada, mas a conta esta em espera e so deve reagir com nova prioridade interna ou gatilho objetivo de retomada."
    if risk == "Alto":
        return "A conta segue viva, mas o risco de dispersao comercial ja e alto e pede requalificacao franca do proximo passo."
    if any(token in combined for token in ("proposta", "contrato", "assinatura")):
        return "A conta esta mais proxima de decisao comercial do que de exploracao, desde que a validacao final avance."
    if "diagnost" in stage:
        return "A conta ainda esta em construcao comercial e precisa sair de conversa exploratoria para um checkpoint concreto."
    return "A conta permanece ativa, mas precisa de um marco mais objetivo para converter contexto em avancada comercial."


def collect_llm_source_evidence(deal: dict, max_items: int = 8) -> list[str]:
    by_tab = deal.get("interacoes_por_aba") or {}
    tab_labels = {
        "observacao": "Observacao",
        "reuniao": "Reuniao",
    }
    tab_limits = {
        "observacao": 5,
        "reuniao": 3,
    }
    evidence = []
    seen = set()
    for tab in ("observacao", "reuniao"):
        for raw_text in (by_tab.get(tab) or [])[: tab_limits.get(tab, 1)]:
            text = summarize_interaction_text(raw_text, limit=900)
            key = normalize_owner_name(text)
            if not text or not key or key in seen:
                continue
            evidence.append(f"{tab_labels.get(tab, tab)}: {text}")
            seen.add(key)
            if len(evidence) >= max_items:
                return evidence
    return evidence


def build_llm_case_brief(deal: dict, index: int) -> str:
    account_name, opportunity_name = parse_deal_title(deal.get("nome", "Sem nome"))
    source_evidence = collect_llm_source_evidence(deal)
    block_lines = [
        f"Negocio #{index}",
        f"Conta: {account_name}",
        f"Oportunidade: {opportunity_name}",
        f"Empresa/Segmento: {deal.get('empresa', '') or account_name}",
        f"Etapa: {deal.get('etapa', '')}",
        "Registros de observacoes e reunioes para resumir:",
        *([f"- {item}" for item in source_evidence] or ["- Sem observacoes ou reunioes registradas para este negocio."]),
    ]
    return "\n".join(block_lines)


def build_deal_by_deal_summary(filtered: list[dict]) -> str:
    if not filtered:
        return "Resumo Executivo de Negocios\nSem negocios para os filtros selecionados."

    owner_names = sorted({(d.get("proprietario") or "").strip() for d in filtered if (d.get("proprietario") or "").strip()})
    lines = [
        "Resumo Executivo de Negocios - Pipeline AIOS",
        f"Responsaveis: {', '.join(owner_names) if owner_names else '-'}",
        f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
    ]

    ordered = sorted(
        filtered,
        key=lambda d: (d.get("etapa", ""), -d.get("valor_num", 0.0), d.get("nome", "")),
    )
    for idx, d in enumerate(ordered, start=1):
        account_name, opportunity_name = parse_deal_title(d.get("nome", "Sem nome"))
        evidence = collect_all_relevant_evidence(d)
        company = normalize_whitespace(d.get("empresa", ""))
        segment_label = company if company and company != "Nao vinculada" else account_name
        product_label = infer_product_label(d, evidence)
        context_text = build_context_text(d, evidence, opportunity_name)
        status_text = build_current_status_text(d, evidence)
        reading_bullets = build_commercial_reading_bullets(d, evidence)
        next_step_bullets = build_next_step_bullets(d, evidence)

        lines.append(f"{idx}. {account_name} - {opportunity_name}")
        lines.append(f"Segmento/Conta: {segment_label or 'Nao informado'}")
        lines.append(f"Produto: {product_label}")
        lines.append(f"Status: {normalize_whitespace(d.get('etapa', 'Sem etapa')) or 'Sem etapa'}")
        if d.get("valor"):
            lines.append(f"Valor: {normalize_whitespace(d.get('valor', '--'))}")
        lines.append("Contexto da demanda")
        lines.append(context_text or "(sem evidencia recente)")
        lines.append("Situacao atual")
        lines.append(status_text)
        lines.append("Leitura comercial")
        for bullet in reading_bullets:
            lines.append(f"- {bullet}")
        lines.append("Proximo passo")
        for bullet in next_step_bullets:
            lines.append(f"- {bullet}")
        lines.append("")

    return "\n".join(lines).strip()


def build_telegram_messages(filtered: list[dict], max_len: int = 3900) -> list[str]:
    if not filtered:
        return ["📊 Visao Comercial - Pipeline AIOS\nSem negocios para os filtros selecionados."]

    ordered = sorted(
        filtered,
        key=lambda d: (d.get("etapa", ""), -d.get("valor_num", 0.0), d.get("nome", "")),
    )
    timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")

    def base_header() -> list[str]:
        return [
            "📊 Visao Comercial - Pipeline AIOS",
            f"📅 {timestamp} | 👤 Responsavel: Erick",
            "",
        ]

    def render_block(idx: int, deal: dict) -> list[str]:
        account_name, opportunity_name = parse_deal_title(deal.get("nome", "Sem nome"))
        evidence = collect_all_relevant_evidence(deal)
        next_step_bullets = build_next_step_bullets(deal, evidence)
        update_text = build_account_update_text(deal, evidence)

        lines = [
            f"🏷️ {account_name} - {opportunity_name}",
            f"📝 {shorten(update_text, 420)}",
        ]
        lines.append(f"🎯 Proximo passo: {shorten(' '.join(next_step_bullets), 180)}")
        lines.append("")
        return lines

    messages = []
    current_lines = base_header()
    deals_in_current = 0

    for idx, deal in enumerate(ordered, start=1):
        block = render_block(idx, deal)
        candidate = "\n".join(current_lines + block).strip()
        if deals_in_current > 0 and len(candidate) > max_len:
            messages.append("\n".join(current_lines).strip())
            current_lines = base_header() + block
            deals_in_current = 1
        else:
            current_lines.extend(block)
            deals_in_current += 1

    if current_lines:
        messages.append("\n".join(current_lines).strip())

    if len(messages) == 1:
        return messages

    total = len(messages)
    numbered = []
    for part_idx, message in enumerate(messages, start=1):
        lines = message.splitlines()
        lines.insert(1, f"Parte {part_idx}/{total}")
        numbered.append("\n".join(lines).strip())
    return numbered


def build_minimax_prompt(filtered: list[dict]) -> str:
    today = date.today()
    owner_names = sorted({(d.get("proprietario") or "").strip() for d in filtered if (d.get("proprietario") or "").strip()})

    blocks = []
    ordered = sorted(filtered, key=lambda d: (d.get("etapa", ""), -d.get("valor_num", 0.0), d.get("nome", "")))
    for idx, d in enumerate(ordered, start=1):
        blocks.append(build_llm_case_brief(d, idx))
    data_blob = "\n\n".join(blocks)

    return (
        "Voce e um analista comercial senior. Sua funcao e resumir fielmente registros comerciais para a alta gestao da empresa.\n\n"
        "OBJETIVO:\n"
        "- Criar um resumo curto por negocio usando somente o que estiver nos registros de observacoes e/ou reunioes.\n"
        "- O resumo deve contar o que foi registrado: contexto conversado, pontos alinhados, duvidas, pendencias citadas, restricoes mencionadas e situacao descrita nos registros.\n"
        "- Nao criar leitura comercial propria. Nao acrescentar acoes, risco, valor, timing, probabilidade de fechamento, BANT ou dias sem atividade.\n"
        f"- Responsaveis do filtro: {', '.join(owner_names) if owner_names else '-'}\n\n"
        "FORMATO OBRIGATORIO DA RESPOSTA (usar emojis, compativel com Telegram e Slack):\n"
        f"- Primeira linha: 📋 Resumo de Observacoes e Reunioes - Pipeline AIOS\n"
        f"- Segunda linha: 📅 {today.strftime('%d/%m/%Y')} | 👤 Responsavel: Erick\n"
        "- Depois disso, va direto para os negocios. Nao criar introducao, snapshot geral, secoes longas, BANT ou recomendacoes.\n"
        "- Para cada negocio, usar EXATAMENTE 2 linhas, separadas por uma linha em branco:\n"
        "  🔹 <Conta> / <Oportunidade>\n"
        "  <paragrafo unico de 2 a 4 frases curtas resumindo apenas o conteudo das observacoes e/ou reunioes>\n"
        "- Se nao houver observacao ou reuniao para um negocio, escrever: Sem observacoes ou reunioes relevantes registradas.\n\n"
        "REGRAS DE QUALIDADE:\n"
        "- Reescreva em linguagem propria, mas preserve os fatos registrados.\n"
        "- Nao usar informacoes de e-mails, tarefas, valor do negocio, risco calculado, etapa ou dias sem atividade para compor o resumo. Esses campos so ajudam a identificar o negocio.\n"
        "- Nao recomendar proximos passos e nao escrever frases como 'necessario', 'deve', 'precisa', 'recomenda-se' ou 'acao'.\n"
        "- Nao classificar com emojis de risco ou semaforo.\n"
        "- Nao invente conclusoes. Se algo nao estiver nos registros de observacoes/reunioes, nao mencione.\n\n"
        f"Dados por negocio (usar somente a secao de registros de observacoes e reunioes):\n{data_blob}\n"
    )


def split_text_for_telegram(summary_text: str, max_len: int = 3900) -> list[str]:
    text = (summary_text or "").strip()
    if not text:
        return ["Resumo Executivo de Negocios\nSem negocios para os filtros selecionados."]
    if len(text) <= max_len:
        return [text]

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        return [shorten(text, max_len)]

    parts = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(block) <= max_len:
            current = block
            continue
        chunks = [block[i : i + max_len] for i in range(0, len(block), max_len)]
        parts.extend(chunks[:-1])
        current = chunks[-1]

    if current:
        parts.append(current)

    if len(parts) == 1:
        return parts

    total = len(parts)
    numbered = []
    for idx, part in enumerate(parts, start=1):
        lines = part.splitlines()
        if lines:
            lines.insert(1, f"Parte {idx}/{total}")
            numbered.append("\n".join(lines).strip())
        else:
            numbered.append(f"Parte {idx}/{total}")
    return numbered


def build_summary_system_prompt() -> str:
    return (
        "Voce resume registros de observacoes e reunioes comerciais para lideranca. "
        "Sua resposta deve ser objetiva, fiel aos registros e sem inferencias comerciais extras. "
        "Nao acrescente acoes, riscos, valores, timing, probabilidade de fechamento ou proximos passos. "
        "Quando faltar registro, diga apenas que nao ha observacoes ou reunioes relevantes registradas."
    )


def build_director_summary_messages(filtered: list[dict], max_len: int = 3900) -> tuple[list[str], dict]:
    summary_text, meta = generate_summary_with_llm(filtered)
    return split_text_for_telegram(summary_text, max_len=max_len), meta


def generate_summary_with_minimax(filtered: list[dict]) -> tuple[str, dict]:
    api_key = get_env("MINIMAX_API_KEY", "MINIMAX_APIKEY", "Minimax API-KEY")
    api_url = get_env("MINIMAX_API_URL", default="https://api.minimax.chat/v1/text/chatcompletion_v2")
    base_url = get_env("MINIMAX_BASE_URL", "Minimax_BASE_URL")
    model = get_env("MINIMAX_MODEL", "Minimax_MODEL", default="MiniMax-M2.5")

    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY nao encontrado no .env")
    if base_url and "chatcompletion" not in api_url:
        api_url = base_url.rstrip("/") + "/v1/text/chatcompletion_v2"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_summary_system_prompt()},
            {"role": "user", "content": build_minimax_prompt(filtered)},
        ],
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(api_url, headers=headers, json=payload, timeout=90)
    if resp.status_code >= 400:
        raise RuntimeError(f"Erro MiniMax HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    if isinstance(data, dict) and isinstance(data.get("base_resp"), dict):
        base_resp = data.get("base_resp", {})
        status_code = base_resp.get("status_code")
        status_msg = base_resp.get("status_msg", "")
        if status_code not in (0, None):
            raise RuntimeError(f"MiniMax status {status_code}: {status_msg or 'erro de autenticacao/parametros'}")

    if isinstance(data, dict):
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content")
            if content:
                return content, {"source": "minimax", "model": model, "status": "ok"}
        if data.get("reply"):
            return data["reply"], {"source": "minimax", "model": model, "status": "ok"}
        if data.get("output_text"):
            return data["output_text"], {"source": "minimax", "model": model, "status": "ok"}
        if isinstance(data.get("data"), dict):
            if data["data"].get("reply"):
                return data["data"]["reply"], {"source": "minimax", "model": model, "status": "ok"}
            if data["data"].get("text"):
                return data["data"]["text"], {"source": "minimax", "model": model, "status": "ok"}

    keys = list(data.keys())[:12] if isinstance(data, dict) else []
    raise RuntimeError(f"Resposta MiniMax sem texto reconhecido. keys={keys}")


def generate_summary_with_openrouter(filtered: list[dict]) -> tuple[str, dict]:
    api_key = get_env("OPENROUTER_API_KEY")
    model = get_env("OPENROUTER_MODEL", default="openai/gpt-4o-mini-2024-07-18")
    api_url = get_env("OPENROUTER_API_URL", default="https://openrouter.ai/api/v1/chat/completions")

    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY nao encontrado no .env")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_summary_system_prompt()},
            {"role": "user", "content": build_minimax_prompt(filtered)},
        ],
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(api_url, headers=headers, json=payload, timeout=90)
    if resp.status_code >= 400:
        raise RuntimeError(f"Erro OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content:
            return content, {"source": "openrouter", "model": model, "status": "ok"}

    keys = list(data.keys())[:12] if isinstance(data, dict) else []
    raise RuntimeError(f"Resposta OpenRouter sem texto reconhecido. keys={keys}")


def generate_summary_with_llm(filtered: list[dict]) -> tuple[str, dict]:
    # Prioriza OpenRouter se estiver configurado; fallback para MiniMax.
    if get_env("OPENROUTER_API_KEY"):
        return generate_summary_with_openrouter(filtered)
    return generate_summary_with_minimax(filtered)


def send_to_telegram(messages: str | list[str]) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        raise RuntimeError("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payloads = messages if isinstance(messages, list) else [messages]
    for message in payloads:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": shorten(message, 3900),
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Erro Telegram HTTP {resp.status_code}: {resp.text[:300]}")


def send_to_slack(messages: str | list[str]) -> None:
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    channel = os.getenv("SLACK_CHANNEL", "teste").strip()

    if not bot_token or not channel:
        raise RuntimeError("Defina SLACK_BOT_TOKEN e SLACK_CHANNEL no .env")

    url = "https://slack.com/api/chat.postMessage"
    payloads = messages if isinstance(messages, list) else [messages]
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    channel_target = resolve_slack_channel(bot_token, channel)
    for message in payloads:
        resp = requests.post(
            url,
            headers=headers,
            json={
                "channel": channel_target,
                "text": shorten(message, 3900),
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Erro Slack HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Erro Slack API: {data.get('error', 'resposta sem ok')}")


def resolve_slack_channel(bot_token: str, channel: str) -> str:
    value = (channel or "").strip()
    if not value:
        return value
    if re.match(r"^[CGD][A-Z0-9]+$", value):
        return value

    wanted = value.lstrip("#").strip().lower()
    headers = {"Authorization": f"Bearer {bot_token}"}
    cursor = ""
    for _ in range(10):
        params = {
            "exclude_archived": "true",
            "limit": 1000,
            "types": "public_channel,private_channel",
        }
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(
                "https://slack.com/api/conversations.list",
                headers=headers,
                params=params,
                timeout=20,
            )
            data = resp.json()
        except Exception:
            break
        if not data.get("ok"):
            break
        for item in data.get("channels") or []:
            if (item.get("name") or "").strip().lower() == wanted:
                return item.get("id") or value
        cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            break
    return value if value.startswith("#") else f"#{wanted}"


def remember_last_summary(summary_messages: list[str], summary_meta: dict, filters: dict) -> None:
    st.session_state["last_summary"] = ("\n\n" + ("=" * 48) + "\n\n").join(summary_messages)
    st.session_state["last_summary_meta"] = summary_meta
    st.session_state["last_summary_filters"] = filters


def inject_styles():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Manrope:wght@600;700;800&display=swap');

        :root {
          --surface: #f8f9fb;
          --surface-low: #f3f4f6;
          --surface-card: #ffffff;
          --on-surface: #191c1e;
          --secondary: #4f5f7b;
          --primary: #003d9b;
          --primary-container: #0052cc;
        }

        .stApp { background: var(--surface); color: var(--on-surface); font-family: 'Inter', sans-serif; }
        .block-container { padding-top: 1.4rem; padding-left: 1.8rem; padding-right: 1.8rem; max-width: 100%; }

        .title-wrap { background: var(--surface-low); border-radius: 14px; padding: 18px 22px; margin-bottom: 16px; }
        .title-main { font-family: 'Manrope', sans-serif; font-size: 1.35rem; font-weight: 800; margin: 0; color: var(--on-surface); }
        .title-sub { font-size: 0.82rem; color: var(--secondary); margin-top: 2px; }

        .kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: 10px 0 18px; }
        .kpi-card { background: var(--surface-card); border-radius: 14px; padding: 16px 18px; box-shadow: 0px 4px 20px rgba(25, 28, 30, 0.04), 0px 12px 40px rgba(25, 28, 30, 0.08); }
        .kpi-primary { background: linear-gradient(135deg, var(--primary), var(--primary-container)); color: #fff; }
        .kpi-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: .06em; opacity: .9; }
        .kpi-value { font-family: 'Manrope', sans-serif; font-size: 1.9rem; font-weight: 800; line-height: 1.05; margin-top: 6px; }
        .kpi-note { font-size: 0.73rem; margin-top: 6px; color: var(--secondary); }

        .panel { background: var(--surface-card); border-radius: 14px; padding: 18px; box-shadow: 0px 4px 20px rgba(25, 28, 30, 0.04); height: 100%; }
        .panel-title { font-family: 'Manrope', sans-serif; font-size: 1rem; font-weight: 700; margin-bottom: 12px; }
        .stage-row { margin: 10px 0; }
        .stage-head { display:flex; justify-content:space-between; font-size:.83rem; margin-bottom:4px; }
        .bar-wrap { background: var(--surface-low); border-radius: 999px; height: 8px; overflow:hidden; }
        .bar { background: linear-gradient(135deg, var(--primary), var(--primary-container)); height: 8px; }

        .chip { display:inline-block; padding: 3px 10px; border-radius: 999px; font-size: .68rem; font-weight: 600; background: #dae2ff; color: #0040a2; }
        .chip-risk-alto { background: #ffdad6; color: #93000a; }
        .chip-risk-medio { background: #ffe3b3; color: #8b5a00; }

        .summary-card {
          background: #ffffff;
          border-radius: 14px;
          padding: 20px;
          box-shadow: 0px 4px 20px rgba(25, 28, 30, 0.04);
          white-space: pre-wrap;
          line-height: 1.45;
          font-size: .96rem;
        }

        @media (max-width: 1000px) { .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
        @media (max-width: 640px) { .kpi-grid { grid-template-columns: 1fr; } }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_kpis(filtered):
    total_value = sum(d["valor_num"] for d in filtered)
    high_risk = sum(1 for d in filtered if d["risco"] == "Alto")
    medium_risk = sum(1 for d in filtered if d["risco"] == "Medio")

    html = f"""
    <div class='kpi-grid'>
      <div class='kpi-card kpi-primary'>
        <div class='kpi-label'>Negocios</div>
        <div class='kpi-value'>{len(filtered)}</div>
      </div>
      <div class='kpi-card'>
        <div class='kpi-label'>Pipeline</div>
        <div class='kpi-value' style='font-size:1.55rem;color:#003d9b'>{format_brl(total_value)}</div>
        <div class='kpi-note'>Valor total monitorado</div>
      </div>
      <div class='kpi-card'>
        <div class='kpi-label'>Risco Alto</div>
        <div class='kpi-value' style='color:#ba1a1a'>{high_risk}</div>
      </div>
      <div class='kpi-card'>
        <div class='kpi-label'>Risco Medio</div>
        <div class='kpi-value' style='color:#b26a00'>{medium_risk}</div>
      </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


def main():
    st.set_page_config(page_title="Curator Pro | HubSpot Commercial", layout="wide")
    inject_styles()

    data = enrich(load_data(DATA_FILE))
    stages_from_data = sorted({d["etapa"] for d in data if not is_excluded_stage(d.get("etapa", ""))})
    owners_from_data = sorted({d["proprietario"] for d in data})
    if INCLUDED_STAGE_LABELS:
        stage_map = {
            normalize_owner_name(s): s
            for s in INCLUDED_STAGE_LABELS
            if not is_excluded_stage(s)
        }
        for stage in stages_from_data:
            norm = normalize_owner_name(stage)
            if norm not in stage_map:
                stage_map[norm] = stage
        stages = list(stage_map.values())
    else:
        stages = stages_from_data
    owners = owners_from_data or DEFAULT_OWNERS
    owner_options = [ALL_OWNERS_LABEL] + owners
    risks = ["Alto", "Medio", "Baixo", "Sem informacao"]

    default_stages = [s for s in stages if is_included_stage(s)]
    if not default_stages:
        default_stages = stages

    default_owner_norms = {normalize_owner_name(o) for o in DEFAULT_OWNERS}
    default_owners = [o for o in owners if normalize_owner_name(o) in default_owner_norms]
    if not default_owners:
        default_owners = owners[:1] if owners else []
    if "openrouter_model_input" not in st.session_state:
        st.session_state["openrouter_model_input"] = get_env(
            "OPENROUTER_MODEL", default="openai/gpt-4o-mini-2024-07-18"
        )

    with st.sidebar:
        st.header("Filtros")
        sel_stages = st.multiselect("Etapa", stages, default=default_stages)
        sel_owners = st.multiselect("Responsavel", owner_options, default=default_owners)
        sel_risks = st.multiselect("Risco", risks, default=risks)
        st.caption(f"Padrao inicial: responsavel {TARGET_OWNER_NAME}. Voce pode ajustar os filtros livremente.")
        st.divider()
        st.subheader("Atualizacao")
        refresh_btn = st.button("Atualizar dados do HubSpot", use_container_width=True)
        refresh_session_btn = st.button("Renovar sessao do HubSpot", use_container_width=True)
        if running_inside_docker():
            st.caption("No Docker, a renovacao interativa nao abre janela. Execute `python script.py --refresh-session` no host.")
        else:
            st.caption("Abre o login manual do HubSpot e executa `python script.py --refresh-session` neste ambiente.")
        st.divider()
        st.subheader("Resumo Executivo")
        send_telegram_btn = st.button("Gerar resumo e enviar Telegram", use_container_width=True, disabled=not bool(data))
        send_slack_btn = st.button("Gerar resumo e enviar Slack", use_container_width=True, disabled=not bool(data))
        st.caption("Briefing executivo estruturado por negocio, gerado por LLM a partir do contexto comercial capturado.")
        st.caption("Variaveis no .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SLACK_BOT_TOKEN, SLACK_CHANNEL")
        st.divider()
        st.subheader("Modelo LLM")
        st.caption(f"Modelo configurado agora: {preferred_summary_backend_label()}")
        st.text_input(
            "OPENROUTER_MODEL",
            key="openrouter_model_input",
            help="Exemplo: openai/gpt-4o-mini-2024-07-18",
        )
        if st.button("Salvar modelo", use_container_width=True):
            model_value = (st.session_state.get("openrouter_model_input") or "").strip()
            if not model_value:
                st.error("Informe um modelo valido antes de salvar.")
            else:
                try:
                    upsert_env_value("OPENROUTER_MODEL", model_value)
                    st.success(f"Modelo salvo no .env: {model_value}")
                except Exception as exc:
                    st.error(f"Nao foi possivel salvar OPENROUTER_MODEL: {exc}")
        st.caption("A alteracao persiste no arquivo .env e sera usada nos proximos resumos.")

    filtered = [
        d
        for d in data
        if d["etapa"] in sel_stages
        and (ALL_OWNERS_LABEL in sel_owners or d["proprietario"] in sel_owners)
        and d["risco"] in sel_risks
        and not is_excluded_stage(d.get("etapa", ""))
    ]
    quality_issues = [d for d in filtered if deal_quality_flags(d)]

    st.markdown(
        """
        <div class='title-wrap'>
          <p class='title-main'>Commercial Command Center</p>
          <div class='title-sub'>Arquitetura editorial baseada em camadas tonais (code.html + DESIGN.md)</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if refresh_btn:
        if not sel_stages or not sel_owners:
            st.error("Selecione ao menos uma etapa e um responsavel para atualizar os dados.")
        else:
            selected_owners_for_refresh = [o for o in sel_owners if o != ALL_OWNERS_LABEL]
            with st.spinner("Executando coleta no HubSpot conforme filtros selecionados..."):
                ok, message = refresh_data_via_script(sel_stages, selected_owners_for_refresh, sel_risks)
            if ok:
                st.success("Dados atualizados com sucesso. Recarregando dashboard...")
                st.session_state["refresh_log"] = message
                st.rerun()
            else:
                st.error(message)
                st.session_state["refresh_log"] = message

    if refresh_session_btn:
        with st.spinner("Abrindo HubSpot para renovar a sessao manualmente..."):
            ok, message = refresh_hubspot_session_via_script()
        st.session_state["refresh_session_log"] = message
        if ok:
            st.success("Sessao do HubSpot renovada com sucesso.")
        else:
            st.error(message)

    if st.session_state.get("refresh_log"):
        st.markdown("### Log da atualiza??o")
        st.text_area("Saida da ultima execucao", st.session_state["refresh_log"], height=160)

    if st.session_state.get("refresh_session_log"):
        st.markdown("### Log da renovacao de sessao")
        st.text_area("Saida da ultima renovacao", st.session_state["refresh_session_log"], height=160)

    if not data:
        st.error("Arquivo resumos_hubspot.json nao encontrado ou vazio.")
        return

    summary_filters = {
        "stages": sorted(sel_stages),
        "owners": sorted(sel_owners),
        "risks": sorted(sel_risks),
    }

    if send_telegram_btn or send_slack_btn:
        if not filtered:
            st.warning("Nao ha negocios no filtro atual para gerar resumo.")
        else:
            destination = "Telegram" if send_telegram_btn else "Slack"
            with st.spinner(f"Gerando resumo negocio a negocio e enviando para {destination}..."):
                try:
                    summary_messages, summary_meta = build_director_summary_messages(filtered)
                    if send_telegram_btn:
                        send_to_telegram(summary_messages)
                        st.success("Resumo enviado para o Telegram com sucesso.")
                    else:
                        send_to_slack(summary_messages)
                        st.success("Resumo enviado para o Slack com sucesso.")
                except Exception as exc:
                    st.error(f"Falha ao gerar ou enviar resumo para {destination}: {exc}")
                else:
                    remember_last_summary(summary_messages, summary_meta, summary_filters)

    current_filters = summary_filters
    if st.session_state.get("last_summary_filters") != current_filters:
        st.session_state.pop("last_summary", None)
        st.session_state.pop("last_summary_meta", None)

    if st.session_state.get("last_summary"):
        st.markdown("### Ultimo resumo gerado")
        summary_meta = st.session_state.get("last_summary_meta") or {}
        if summary_meta:
            source = summary_meta.get("source", "-")
            model = summary_meta.get("model", "-")
            status = summary_meta.get("status", "")
            st.caption(f"Fonte do resumo: {source} | Modelo: {model}" + (f" | {status}" if status else ""))
        st.markdown(
            f"<div class='summary-card'>{escape(st.session_state['last_summary'])}</div>",
            unsafe_allow_html=True,
        )

    render_kpis(filtered)

    if quality_issues:
        st.warning(
            f"Foram detectados {len(quality_issues)} negocio(s) com sinais de coleta inconsistente. "
            "Revise a ultima atualizacao antes de usar essas linhas no resumo."
        )

    stage_map = {}
    for d in filtered:
        row = stage_map.setdefault(d["etapa"], {"Etapa": d["etapa"], "Negocios": 0, "Valor": 0.0})
        row["Negocios"] += 1
        row["Valor"] += d["valor_num"]
    stage_rows = sorted(stage_map.values(), key=lambda r: r["Negocios"], reverse=True)

    top = sorted(filtered, key=lambda d: d["valor_num"], reverse=True)[:10]
    max_stage_value = max((r["Valor"] for r in stage_rows), default=1)

    col1, col2 = st.columns([5, 7])

    with col1:
        st.markdown("<div class='panel'><div class='panel-title'>Pipeline por etapa</div>", unsafe_allow_html=True)
        if not stage_rows:
            st.write("Sem dados para os filtros atuais.")
        else:
            for row in stage_rows:
                width = (row["Valor"] / max_stage_value) * 100 if max_stage_value else 0
                st.markdown(
                    f"""
                    <div class='stage-row'>
                      <div class='stage-head'>
                        <span>{row['Etapa']}</span>
                        <span>{format_brl(row['Valor'])} ({row['Negocios']})</span>
                      </div>
                      <div class='bar-wrap'><div class='bar' style='width:{width:.2f}%'></div></div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        st.markdown("</div>", unsafe_allow_html=True)

    with col2:
        st.markdown("<div class='panel'><div class='panel-title'>Top negocios por valor</div>", unsafe_allow_html=True)
        if not top:
            st.write("Sem dados para os filtros atuais.")
        else:
            for d in top:
                risk_cls = "chip"
                if d["risco"] == "Alto":
                    risk_cls = "chip chip-risk-alto"
                elif d["risco"] == "Medio":
                    risk_cls = "chip chip-risk-medio"
                st.markdown(
                    f"""
                    <div style='padding:10px 0'>
                      <div style='font-weight:600'>{d['nome']}</div>
                      <div style='font-size:.82rem;color:#4f5f7b;margin-top:2px'>
                        {d['etapa']} | {format_brl(d['valor_num'])} | {d['proprietario']}
                      </div>
                      <div style='margin-top:6px'><span class='{risk_cls}'>{d['risco']}</span></div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("### Carteira e Interacoes")
    st.dataframe(
        [
            {
                "Negocio": d["nome"] or "-- dado inconsistente --",
                "Empresa": d["empresa"] or "--",
                "Etapa": d["etapa"],
                "Valor": d["valor"] or "--",
                "Responsavel": d["proprietario"],
                "Ultima atividade": d["ultima_atividade"] or "--",
                "Dias sem atividade": d["dias_sem_atividade"] if d["dias_sem_atividade"] is not None else "--",
                "Risco": d["risco"],
                "Qualidade": ", ".join(deal_quality_flags(d)) or "ok",
                "Observacao": shorten(d["interacoes"].get("observacao") or "--", 220),
                "E-mail": shorten(d["interacoes"].get("email") or "--", 220),
                "Tarefa": shorten(d["interacoes"].get("tarefa") or "--", 220),
                "Reuniao": shorten(d["interacoes"].get("reuniao") or "--", 220),
                "URL": d["url"],
            }
            for d in sorted(filtered, key=lambda x: x["valor_num"], reverse=True)
        ],
        use_container_width=True,
        hide_index=True,
    )


if __name__ == "__main__":
    main()
