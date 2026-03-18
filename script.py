from pathlib import Path
from urllib.parse import urlparse
import json
import os
import re
import time
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

HUBSPOT_BASE_URL = "https://app.hubspot.com"
STORAGE_STATE_PATH = Path("hubspot-session.json")
OUTPUT_PATH = Path("resumos_hubspot.json")
WEEKLY_REPORT_PATH = Path("resumo_comercial_semanal.md")


def get_portal_id() -> str:
    portal_id = os.getenv("HUBSPOT_PORTAL_ID", "").strip()
    if not portal_id:
        raise RuntimeError(
            "Defina a variavel de ambiente HUBSPOT_PORTAL_ID com o ID do seu portal HubSpot."
        )
    return portal_id


def deals_list_url(portal_id: str) -> str:
    custom_url = os.getenv("HUBSPOT_DEALS_URL", "").strip()
    if custom_url:
        return custom_url
    return f"{HUBSPOT_BASE_URL}/contacts/{portal_id}/objects/0-3/views/all/list"


def is_authenticated_on_portal(page, portal_id: str) -> bool:
    parsed = urlparse(page.url)
    if parsed.netloc.lower() != "app.hubspot.com":
        return False
    if f"/contacts/{portal_id}/" not in parsed.path:
        return False
    return "/login" not in parsed.path.lower()


def ensure_authenticated(page, list_url: str, portal_id: str, storage_path: Path) -> None:
    page.goto(list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    if is_authenticated_on_portal(page, portal_id):
        return

    print("Sessao invalida ou ausente. Faca login manualmente na janela do navegador.")
    print("Aguardando retorno ao HubSpot (timeout: 10 minutos)...")

    deadline = time.time() + 600
    while time.time() < deadline:
        if is_authenticated_on_portal(page, portal_id):
            page.context.storage_state(path=str(storage_path))
            print(f"Sessao salva em: {storage_path}")
            return
        page.wait_for_timeout(2000)

    raise RuntimeError("Login nao confirmado no HubSpot dentro de 10 minutos.")


def collect_deal_links(page, list_url: str):
    page.goto(list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    links = page.evaluate(
        """() => {
            const patterns = ["/record/0-3/", "/objects/0-3/record/", "/deal/"];
            const anchors = Array.from(document.querySelectorAll("a[href]"));
            const found = anchors
                .map(a => a.href)
                .filter(href => patterns.some(p => href.includes(p)));
            return [...new Set(found)];
        }"""
    )

    if not links:
        page.wait_for_timeout(5000)
        links = page.evaluate(
            """() => {
                const patterns = ["/record/0-3/", "/objects/0-3/record/", "/deal/"];
                const anchors = Array.from(document.querySelectorAll("a[href]"));
                const found = anchors
                    .map(a => a.href)
                    .filter(href => patterns.some(p => href.includes(p)));
                return [...new Set(found)];
            }"""
        )

    if not links:
        html_debug_path = Path("hubspot_deals_debug.html")
        html_debug_path.write_text(page.content(), encoding="utf-8")
        raise RuntimeError(
            "Nenhum link de negocio encontrado. Verifique o acesso ao pipeline e os seletores da pagina. "
            "HTML salvo em hubspot_deals_debug.html para depuracao."
        )

    canonical_links = []
    seen = set()
    for link in links:
        clean = link.split("?", 1)[0].split("#", 1)[0]
        if clean not in seen:
            seen.add(clean)
            canonical_links.append(clean)

    return canonical_links


def safe_text(locator, default=""):
    try:
        if locator.count() > 0:
            return locator.first.inner_text().strip()
    except Exception:
        pass
    return default


def text_from_test_id(page, test_id: str, default="") -> str:
    locator = page.locator(f"[data-test-id='{test_id}']")
    if locator.count() == 0:
        return default
    try:
        value = locator.first.evaluate(
            """el => {
                const input = el.querySelector("input");
                if (input && input.value) return input.value.trim();
                return (el.innerText || el.textContent || "").trim();
            }"""
        )
        return value or default
    except Exception:
        return default


def cleanup_value(value: str, label_prefixes: list[str]) -> str:
    if not value:
        return ""
    cleaned = " ".join(value.split())
    for prefix in label_prefixes:
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix) :].strip(" :")
    return cleaned


def classify_interaction(text: str) -> str:
    low = text.lower()
    if "reuni" in low:
        return "reuniao"
    if "e-mail" in low or "email" in low:
        return "email"
    if "tarefa" in low:
        return "tarefa"
    if "observa" in low or "nota" in low:
        return "observacao"
    return "atividade"


def normalize_tab_name(text: str) -> str:
    value = text.lower().strip()
    repl = {
        "á": "a",
        "à": "a",
        "â": "a",
        "ã": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
        "-": "",
        " ": "",
    }
    for src, dst in repl.items():
        value = value.replace(src, dst)
    return value


def ensure_activities_view(page) -> None:
    page.evaluate(
        """() => {
            const norm = (s) => (s || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .trim();
            const nodes = Array.from(document.querySelectorAll("button, a, [role='tab']"));
            const btn = nodes.find(n => norm(n.textContent) === "atividades");
            if (btn) btn.click();
        }"""
    )
    page.wait_for_timeout(1200)


def switch_activity_tab(page, tab_key: str) -> bool:
    aliases = {
        "atividade": ["atividade"],
        "observacao": ["observacoes", "observacao"],
        "email": ["emails", "email", "emails"],
        "tarefa": ["tarefas", "tarefa"],
        "reuniao": ["reunioes", "reuniao"],
    }
    possible = aliases.get(tab_key, [tab_key])
    return bool(
        page.evaluate(
            """(names) => {
                const norm = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[-\\s]/g, "");
                const targets = names.map(norm);
                const scope = document.querySelector("[data-test-id='crm-events-viz-timeline']") || document;
                const nodes = Array.from(scope.querySelectorAll("button, a, [role='tab']"));
                const found = nodes.find(n => {
                    const txt = norm(n.textContent);
                    return targets.some(t => txt.includes(t));
                });
                if (!found) return false;
                found.click();
                return true;
            }""",
            possible,
        )
    )


def expand_all_activities(page) -> None:
    page.evaluate(
        """() => {
            const norm = (s) => (s || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .trim();
            const nodes = Array.from(document.querySelectorAll("button, a"));
            const btn = nodes.find(n => norm(n.textContent).includes("expandir tudo"));
            if (btn) btn.click();
        }"""
    )
    page.wait_for_timeout(800)


def extract_timeline_events(page, limit: int = 5):
    return page.evaluate(
        """(limit) => {
            const cards = Array.from(document.querySelectorAll("[data-selenium-test='timeline-card']"));
            return cards.slice(0, limit).map((card) => {
                const header = card.querySelector("[data-test-id='header-message']")?.innerText || "";
                const timestamp = card.querySelector("[data-test-id='event-timestamp']")?.innerText || "";
                const text = (card.innerText || card.textContent || "").replace(/\\s+/g, " ").trim();
                return { text, header, timestamp };
            }).filter(x => x.text);
        }""",
        limit,
    )


def clean_event_text(raw: str, tab: str) -> str:
    text = " ".join((raw or "").split())
    if not text:
        return ""

    if "Edite o seguinte texto:" in text:
        text = text.rsplit("Edite o seguinte texto:", 1)[-1].strip()

    noise_patterns = [
        r"\bAções\b",
        r"Ver ações para esta atividade\.?",
        r"Esta atividade é totalmente expandida\.?",
        r"Clique para recolher esta atividade e ocultar alguns de seus detalhes\.?",
        r"Visualização de conteúdo de [A-Za-zÀ-ÿ-]+",
        r"Clique neste botão para atualizar as associações desta atividade\.?",
        r"Adicionar comentário",
        r"\b\d+\s+associa(?:ção|ções)\b.*$",
        r"Pressione para fixar esta atividade no topo da linha do tempo\.?",
        r"\bFixar\b",
        r"\bCopiar link\b",
    ]
    for pat in noise_patterns:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text).strip(" .|-")

    if tab == "email":
        return text[:1500]
    return text[:700]


def is_meaningful_interaction_text(text: str) -> bool:
    if not text:
        return False
    cleaned = " ".join(text.split()).strip().lower()
    if len(cleaned) < 18:
        return False
    blocked = {
        "expandir",
        "adicionar comentário",
        "adicionar comentario",
        "acoes",
        "atividade",
    }
    if cleaned in blocked:
        return False
    if cleaned.startswith("observação de ") or cleaned.startswith("observacao de "):
        return False
    return True


def collect_recent_interactions(page, limit: int = 5):
    page.wait_for_timeout(1500)
    page.mouse.wheel(0, 1800)
    page.wait_for_timeout(1200)
    ensure_activities_view(page)

    tabs = ["atividade", "observacao", "email", "tarefa", "reuniao"]
    by_tab = {}
    for tab in tabs:
        clicked = switch_activity_tab(page, tab)
        page.wait_for_timeout(1200)
        if clicked:
            expand_all_activities(page)
        events = extract_timeline_events(page, limit=limit)
        cleaned = []
        for e in events:
            event_text = clean_event_text(e.get("text", ""), tab)
            if not event_text:
                header = " ".join((e.get("header", "") or "").split())
                ts = " ".join((e.get("timestamp", "") or "").split())
                event_text = f"{header} {ts}".strip()
            if is_meaningful_interaction_text(event_text):
                cleaned.append(event_text)
        by_tab[tab] = cleaned

    merged = []
    for tab in tabs:
        for text in by_tab.get(tab, []):
            merged.append({"tipo": tab, "texto": text})

    latest_by_type = {
        "atividade": (by_tab.get("atividade") or [""])[0] if by_tab.get("atividade") else "",
        "observacao": (by_tab.get("observacao") or [""])[0] if by_tab.get("observacao") else "",
        "email": (by_tab.get("email") or [""])[0] if by_tab.get("email") else "",
        "tarefa": (by_tab.get("tarefa") or [""])[0] if by_tab.get("tarefa") else "",
        "reuniao": (by_tab.get("reuniao") or [""])[0] if by_tab.get("reuniao") else "",
    }

    if not latest_by_type["atividade"] and merged:
        latest_by_type["atividade"] = merged[0]["texto"]

    return {
        "lista": merged,
        "por_aba": by_tab,
        "ultimas_por_tipo": latest_by_type,
    }


def collect_deal_details(page, url: str):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    name = cleanup_value(
        text_from_test_id(page, "highlight-property-display-dealname")
        or safe_text(page.locator("h1")),
        [],
    )
    stage = cleanup_value(
        text_from_test_id(page, "property-input-dealstage")
        or text_from_test_id(page, "highlight-property-item-dealstage"),
        ["Etapa do negócio", "Etapa do negocio"],
    )
    value = cleanup_value(
        text_from_test_id(page, "highlight-property-display-amount")
        or text_from_test_id(page, "highlight-property-item-amount"),
        ["Valor"],
    )
    owner = cleanup_value(
        text_from_test_id(page, "hubspot_owner_id"),
        ["Proprietário do negócio", "Proprietario do negocio", "Detalhes"],
    )
    last_activity = cleanup_value(
        text_from_test_id(page, "notes_last_contacted"),
        ["Data da última atividade", "Data da ultima atividade", "Último contato", "Ultimo contato", "Detalhes"],
    )

    company = cleanup_value(
        text_from_test_id(page, "associatedcompanyid"),
        ["Empresas", "Empresa"],
    )
    if not company:
        company = safe_text(
            page.locator("[id='card-wrapper-ASSOCIATION_TABLE/0-2'] a[href*='/record/0-2/']")
        )

    interactions = collect_recent_interactions(page, limit=10)

    return {
        "url": url,
        "nome": name,
        "empresa": company,
        "etapa": stage,
        "valor": value,
        "proprietario": owner,
        "ultima_atividade": last_activity,
        "ultimas_interacoes": interactions,
    }


def build_summary(item: dict) -> str:
    return (
        f"Negocio: {item.get('nome', '')}\\n"
        f"Empresa: {item.get('empresa', '')}\\n"
        f"Etapa: {item.get('etapa', '')}\\n"
        f"Valor: {item.get('valor', '')}\\n"
        f"Responsavel: {item.get('proprietario', '')}\\n"
        f"Ultima atividade: {item.get('ultima_atividade', '')}\\n"
    )


def format_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


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


def build_weekly_report(items: list[dict]) -> str:
    today = datetime.now().date()
    stage_totals = {}
    stage_counts = {}
    total_value = 0.0

    enriched = []
    for deal in items:
        amount = parse_brl_to_float(deal.get("valor", ""))
        stage = deal.get("etapa", "").strip() or "Sem etapa"
        last_activity_date = parse_activity_date(deal.get("ultima_atividade", ""))
        days_without_activity = None
        if last_activity_date:
            days_without_activity = (today - last_activity_date).days

        total_value += amount
        stage_totals[stage] = stage_totals.get(stage, 0.0) + amount
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

        enriched.append(
            {
                **deal,
                "valor_num": amount,
                "dias_sem_atividade": days_without_activity,
                "risco": risk_status(days_without_activity),
            }
        )

    enriched.sort(key=lambda d: d.get("valor_num", 0.0), reverse=True)

    at_risk = [d for d in enriched if d.get("risco") in {"Alto", "Medio"}]
    at_risk.sort(
        key=lambda d: (
            d.get("dias_sem_atividade")
            if d.get("dias_sem_atividade") is not None
            else -1
        ),
        reverse=True,
    )

    lines = []
    lines.append("# Resumo Comercial Semanal")
    lines.append("")
    lines.append(f"Data de geracao: {today.strftime('%d/%m/%Y')}")
    lines.append("")
    lines.append("## Visao executiva")
    lines.append(f"- Total de negocios no relatorio: {len(enriched)}")
    lines.append(f"- Valor total do pipeline monitorado: {format_brl(total_value)}")
    lines.append(f"- Negocios com risco (sem atividade > 7 dias): {len(at_risk)}")
    lines.append("")
    lines.append("## Pipeline por etapa")
    for stage, count in sorted(stage_counts.items(), key=lambda kv: kv[1], reverse=True):
        stage_value = stage_totals.get(stage, 0.0)
        lines.append(f"- {stage}: {count} negocio(s) | {format_brl(stage_value)}")
    lines.append("")
    lines.append("## Top negocios por valor")
    if not enriched:
        lines.append("- Nenhum negocio encontrado.")
    else:
        for deal in enriched[:10]:
            lines.append(
                f"- {deal.get('nome', 'Sem nome')} | Etapa: {deal.get('etapa', 'Sem etapa')} | Valor: {format_brl(deal.get('valor_num', 0.0))} | Responsavel: {deal.get('proprietario', '')}"
            )
    lines.append("")
    lines.append("## Negocios com alerta")
    if not at_risk:
        lines.append("- Nenhum negocio com alerta de inatividade.")
    else:
        for deal in at_risk:
            days = deal.get("dias_sem_atividade")
            days_txt = f"{days} dia(s)" if days is not None else "sem data"
            lines.append(
                f"- {deal.get('nome', 'Sem nome')} | Risco: {deal.get('risco')} | Sem atividade: {days_txt} | Ultima atividade: {deal.get('ultima_atividade', '')}"
            )
    lines.append("")
    lines.append("## Resumo por negocio")
    for deal in enriched:
        days = deal.get("dias_sem_atividade")
        days_txt = f"{days} dia(s)" if days is not None else "sem data"
        latest = (deal.get("ultimas_interacoes") or {}).get("ultimas_por_tipo", {})
        lines.append(
            f"- {deal.get('nome', 'Sem nome')} | Empresa: {deal.get('empresa', '') or 'Nao vinculada'} | Etapa: {deal.get('etapa', '')} | Valor: {deal.get('valor', '') or '--'} | Responsavel: {deal.get('proprietario', '')} | Risco: {deal.get('risco')} | Sem atividade: {days_txt}"
        )
        lines.append(
            f"  Interacoes: Atividade={latest.get('atividade', '') or '--'} | Observacao={latest.get('observacao', '') or '--'} | E-mail={latest.get('email', '') or '--'} | Tarefa={latest.get('tarefa', '') or '--'} | Reuniao={latest.get('reuniao', '') or '--'}"
        )

    return "\n".join(lines) + "\n"


def main():
    portal_id = get_portal_id()
    list_url = deals_list_url(portal_id)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context_kwargs = {}
        if STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        try:
            ensure_authenticated(page, list_url, portal_id, STORAGE_STATE_PATH)
            links = collect_deal_links(page, list_url)
        except PlaywrightTimeoutError as exc:
            browser.close()
            raise RuntimeError("Timeout ao carregar paginas do HubSpot.") from exc

        results = []
        for link in links[:20]:
            try:
                data = collect_deal_details(page, link)
                data["resumo"] = build_summary(data)
                results.append(data)
                print("=" * 80)
                print(data["resumo"])
            except Exception as exc:
                print(f"Erro ao processar negocio {link}: {exc}")

        with OUTPUT_PATH.open("w", encoding="utf-8") as fp:
            json.dump(results, fp, ensure_ascii=False, indent=2)

        weekly_report = build_weekly_report(results)
        WEEKLY_REPORT_PATH.write_text(weekly_report, encoding="utf-8")

        print(f"Arquivo gerado: {OUTPUT_PATH}")
        print(f"Arquivo gerado: {WEEKLY_REPORT_PATH}")
        browser.close()


if __name__ == "__main__":
    main()
