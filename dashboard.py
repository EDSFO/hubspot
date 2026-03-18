import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from html import escape
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

DATA_FILE = Path("resumos_hubspot.json")
ENV_FILE = Path(".env")
load_dotenv()


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


def load_data(path: Path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_data_via_script(timeout_sec: int = 1800) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, "script.py"],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return False, "Timeout ao executar script.py. Verifique login no HubSpot e conectividade."
    except Exception as exc:
        return False, f"Falha ao executar script.py: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        msg = stderr if stderr else stdout
        return False, f"script.py retornou erro (code {result.returncode}): {msg[:1400]}"

    output = (result.stdout or "Atualizacao concluida com sucesso.").strip()
    return True, output[-3000:]


def enrich(items: list[dict]):
    today = date.today()
    enriched = []
    for item in items:
        amount = parse_brl_to_float(item.get("valor", ""))
        activity_date = parse_activity_date(item.get("ultima_atividade", ""))
        days_without = (today - activity_date).days if activity_date else None
        inter_root = item.get("ultimas_interacoes") or {}
        inter = inter_root.get("ultimas_por_tipo", {})
        by_tab = inter_root.get("por_aba", {}) if isinstance(inter_root, dict) else {}
        enriched.append(
            {
                **item,
                "valor_num": amount,
                "dias_sem_atividade": days_without,
                "risco": risk_status(days_without),
                "etapa": item.get("etapa") or "Sem etapa",
                "proprietario": item.get("proprietario") or "Sem responsavel",
                "empresa": item.get("empresa") or "Nao vinculada",
                "interacoes": inter,
                "interacoes_por_aba": by_tab,
            }
        )
    return enriched


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


def build_local_executive_summary(filtered: list[dict]) -> str:
    if not filtered:
        return "Resumo Executivo de Negocios\nSem negocios para os filtros selecionados."

    lines = ["Resumo Executivo de Negocios — Pipeline AIOS", ""]
    ordered = sorted(filtered, key=lambda d: d.get("valor_num", 0.0), reverse=True)
    for idx, d in enumerate(ordered[:8], start=1):
        obs = d["interacoes"].get("observacao") or ""
        email = d["interacoes"].get("email") or ""
        tarefa = d["interacoes"].get("tarefa") or ""
        reuniao = d["interacoes"].get("reuniao") or ""
        contexto = obs or email or tarefa or reuniao or "Sem interacoes recentes detalhadas."

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


def build_minimax_prompt(filtered: list[dict]) -> str:
    def pick_last_three(seq):
        if not seq:
            return []
        return [s for s in seq[:3] if isinstance(s, str) and s.strip()]

    stage_count = {}
    for d in filtered:
        stage = d.get("etapa", "Sem etapa")
        stage_count[stage] = stage_count.get(stage, 0) + 1

    stage_snapshot = "\n".join(
        f"- {k}: {v} negócio(s)" for k, v in sorted(stage_count.items(), key=lambda kv: kv[1], reverse=True)
    )

    blocks = []
    ordered = sorted(filtered, key=lambda d: d.get("valor_num", 0.0), reverse=True)
    for idx, d in enumerate(ordered[:10], start=1):
        by_tab = d.get("interacoes_por_aba", {}) or {}
        obs3 = pick_last_three(by_tab.get("observacao", []))
        email3 = pick_last_three(by_tab.get("email", []))
        reuniao3 = pick_last_three(by_tab.get("reuniao", []))

        blocks.append(
            "\n".join(
                [
                    f"Negocio #{idx}",
                    f"Nome: {d.get('nome', '')}",
                    f"Empresa/Segmento: {d.get('empresa', '')}",
                    f"Etapa/Status: {d.get('etapa', '')}",
                    f"Valor: {d.get('valor', '')}",
                    f"Ultima atividade: {d.get('ultima_atividade', '')}",
                    "Ultimas 3 Observacoes:",
                    *([f"  - {x}" for x in obs3] or ["  - (sem dados)"]),
                    "Ultimos 3 E-mails:",
                    *([f"  - {x}" for x in email3] or ["  - (sem dados)"]),
                    "Ultimas 3 Reunioes:",
                    *([f"  - {x}" for x in reuniao3] or ["  - (sem dados)"]),
                ]
            )
        )
    data_blob = "\n\n".join(blocks)

    return (
        "Voce e um executivo comercial senior. Gere um resumo executivo em portugues brasileiro, "
        "com linguagem clara para diretoria, seguindo o layout da referencia visual enviada.\n\n"
        "Formato obrigatorio da resposta:\n"
        "1) Titulo: 'Resumo Executivo Comercial — Pipeline AIOS'\n"
        "2) Secao '🔥 Visao Geral do Pipeline' com lista por etapa (quantidade e observacao estrategica)\n"
        "3) Secoes por prioridade comercial, no estilo:\n"
        "   - '🧩 1. Fechamento (Curto Prazo)'\n"
        "   - '💰 2. Negociacao (Receita em Disputa)'\n"
        "   - '🔎 3. Diagnostico (Potencial Futuro)'\n"
        "4) Para cada negocio listado, usar subtitulos exatamente:\n"
        "   - Status\n"
        "   - Situacao Atual\n"
        "   - Leitura Comercial\n"
        "   - Proximos passos\n\n"
        "Regras:\n"
        "- Situacao Atual, Leitura Comercial e Proximos passos DEVEM ser baseados nas ultimas 3 Observacoes, 3 E-mails e 3 Reunioes de cada negocio.\n"
        "- Se faltar dado, sinalize explicitamente '(sem evidencia recente)'.\n"
        "- Evite jargao excessivo.\n"
        "- Nao invente informacoes ausentes.\n"
        "- Priorize objetividade e recomendacoes acionaveis.\n"
        "- Use bullets e emojis para escaneabilidade.\n\n"
        f"Snapshot de etapas:\n{stage_snapshot}\n\n"
        f"Dados detalhados por negocio:\n{data_blob}\n"
    )


def generate_summary_with_minimax(filtered: list[dict]) -> str:
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
            {"role": "system", "content": "Voce produz resumos executivos comerciais para diretoria."},
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
                return content
        if data.get("reply"):
            return data["reply"]
        if data.get("output_text"):
            return data["output_text"]
        if isinstance(data.get("data"), dict):
            if data["data"].get("reply"):
                return data["data"]["reply"]
            if data["data"].get("text"):
                return data["data"]["text"]

    keys = list(data.keys())[:12] if isinstance(data, dict) else []
    raise RuntimeError(f"Resposta MiniMax sem texto reconhecido. keys={keys}")


def generate_summary_with_openrouter(filtered: list[dict]) -> str:
    api_key = get_env("OPENROUTER_API_KEY")
    model = get_env("OPENROUTER_MODEL", default="openai/gpt-4o-mini-2024-07-18")
    api_url = get_env("OPENROUTER_API_URL", default="https://openrouter.ai/api/v1/chat/completions")

    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY nao encontrado no .env")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Voce produz resumos executivos comerciais para diretoria."},
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
            return content

    keys = list(data.keys())[:12] if isinstance(data, dict) else []
    raise RuntimeError(f"Resposta OpenRouter sem texto reconhecido. keys={keys}")


def generate_summary_with_llm(filtered: list[dict]) -> str:
    # Prioriza OpenRouter se estiver configurado; fallback para MiniMax.
    if get_env("OPENROUTER_API_KEY"):
        return generate_summary_with_openrouter(filtered)
    return generate_summary_with_minimax(filtered)


def split_for_telegram(text: str, max_len: int = 3900) -> list[str]:
    chunks = []
    current = []
    size = 0
    for line in text.splitlines():
        add = len(line) + 1
        if size + add > max_len and current:
            chunks.append("\n".join(current))
            current = [line]
            size = add
        else:
            current.append(line)
            size += add
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


def send_to_telegram(message: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        raise RuntimeError("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for chunk in split_for_telegram(message):
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Erro Telegram HTTP {resp.status_code}: {resp.text[:300]}")


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
    if not data:
        st.error("Arquivo resumos_hubspot.json nao encontrado ou vazio.")
        st.stop()

    stages = sorted({d["etapa"] for d in data})
    owners = sorted({d["proprietario"] for d in data})
    risks = ["Alto", "Medio", "Baixo", "Sem informacao"]

    with st.sidebar:
        st.header("Filtros")
        sel_stages = st.multiselect("Etapa", stages, default=stages)
        sel_owners = st.multiselect("Responsavel", owners, default=owners)
        sel_risks = st.multiselect("Risco", risks, default=risks)
        st.divider()
        st.subheader("Atualizacao")
        refresh_btn = st.button("Atualizar dados do HubSpot", use_container_width=True)
        st.divider()
        st.subheader("Resumo Executivo")
        send_btn = st.button("Gerar resumo e enviar Telegram", use_container_width=True)
        llm_only = st.toggle("Exigir LLM (sem fallback local)", value=True)
        st.caption("Variaveis no .env: OPENROUTER_API_KEY (ou MINIMAX_API_KEY), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

    filtered = [
        d
        for d in data
        if d["etapa"] in sel_stages
        and d["proprietario"] in sel_owners
        and d["risco"] in sel_risks
    ]

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
        with st.spinner("Executando coleta completa no HubSpot..."):
            ok, message = refresh_data_via_script()
        if ok:
            st.success("Dados atualizados com sucesso. Recarregando dashboard...")
            st.session_state["refresh_log"] = message
            st.rerun()
        else:
            st.error(message)
            st.session_state["refresh_log"] = message

    if st.session_state.get("refresh_log"):
        st.markdown("### Log da atualiza??o")
        st.text_area("Saida da ultima execucao", st.session_state["refresh_log"], height=160)

    if send_btn:
        if not filtered:
            st.warning("Nao ha negocios no filtro atual para gerar resumo.")
        else:
            with st.spinner("Gerando resumo executivo e enviando para Telegram..."):
                summary_text = ""
                try:
                    summary_text = generate_summary_with_llm(filtered)
                except Exception as exc:
                    if llm_only:
                        st.error(f"LLM indisponivel: {exc}")
                        st.stop()
                    st.warning(f"LLM indisponivel ({exc}). Usando resumo local.")
                    summary_text = build_local_executive_summary(filtered)

                try:
                    send_to_telegram(summary_text)
                    st.success("Resumo enviado para o Telegram com sucesso.")
                except Exception as exc:
                    st.error(f"Falha ao enviar para Telegram: {exc}")

                st.session_state["last_summary"] = summary_text

    if st.session_state.get("last_summary"):
        st.markdown("### Ultimo resumo gerado")
        st.markdown(
            f"<div class='summary-card'>{escape(st.session_state['last_summary'])}</div>",
            unsafe_allow_html=True,
        )

    render_kpis(filtered)

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
                "Negocio": d["nome"],
                "Empresa": d["empresa"],
                "Etapa": d["etapa"],
                "Valor": d["valor"] or "--",
                "Responsavel": d["proprietario"],
                "Ultima atividade": d["ultima_atividade"] or "--",
                "Dias sem atividade": d["dias_sem_atividade"] if d["dias_sem_atividade"] is not None else "--",
                "Risco": d["risco"],
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
