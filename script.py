from pathlib import Path
from urllib.parse import urlparse
import argparse
import json
import os
import re
import time
import unicodedata
from datetime import datetime

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

HUBSPOT_BASE_URL = "https://app.hubspot.com"
STORAGE_STATE_PATH = Path("hubspot-session.json")
OUTPUT_PATH = Path("resumos_hubspot.json")
WEEKLY_REPORT_PATH = Path("resumo_comercial_semanal.md")
load_dotenv()
TARGET_OWNER_NAME = os.getenv("HUBSPOT_OWNER_NAME", "Erick Douglas Sousa de Freitas Oliveira").strip()
TARGET_OWNER_NAMES_RAW = os.getenv("HUBSPOT_OWNER_NAMES", "").strip()
INCLUDED_STAGES_RAW = os.getenv("HUBSPOT_INCLUDED_STAGES", "Em diagnóstico,Em negociação").strip()
INCLUDED_RISKS_RAW = os.getenv("HUBSPOT_INCLUDED_RISKS", "").strip()
try:
    MAX_TARGET_DEALS = max(1, int(os.getenv("HUBSPOT_MAX_DEALS", "1000")))
except ValueError:
    MAX_TARGET_DEALS = 1000


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "sim"}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


TIMELINE_INITIAL_WAIT_MS = env_int("HUBSPOT_TIMELINE_INITIAL_WAIT_MS", 3000, 500)
TIMELINE_TAB_WAIT_MS = env_int("HUBSPOT_TIMELINE_TAB_WAIT_MS", 2200, 500)
TIMELINE_EMPTY_RETRY_WAIT_MS = env_int("HUBSPOT_TIMELINE_EMPTY_RETRY_WAIT_MS", 3500, 1000)
INTERACTION_COLLECTION_ATTEMPTS = env_int("HUBSPOT_INTERACTION_COLLECTION_ATTEMPTS", 3, 2)


def browser_launch_kwargs() -> dict:
    args = ["--disable-dev-shm-usage"]
    if env_flag("HUBSPOT_CHROMIUM_NO_SANDBOX", default=False):
        args.append("--no-sandbox")
    return {
        "headless": env_flag("HUBSPOT_HEADLESS", default=False),
        "args": args,
    }


def interactive_browser_launch_kwargs() -> dict:
    args = ["--disable-dev-shm-usage"]
    if env_flag("HUBSPOT_CHROMIUM_NO_SANDBOX", default=False):
        args.append("--no-sandbox")
    return {
        "headless": False,
        "args": args,
    }


def normalize_text(value: str) -> str:
    text = (value or "").strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def parse_included_stages(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        norm = normalize_text(part)
        tokens = norm.split()
        is_lost = any(t.startswith("perdid") for t in tokens) or ("closed" in tokens and "lost" in tokens)
        if norm:
            if is_lost:
                continue
            out.append(norm)
    return out


INCLUDED_STAGES = parse_included_stages(INCLUDED_STAGES_RAW)
INCLUDED_RISKS = parse_included_stages(INCLUDED_RISKS_RAW)


def parse_owner_names(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        value = part.strip()
        if value:
            out.append(value)
    return out


if TARGET_OWNER_NAMES_RAW.strip().lower() in {"*", "all", "todos"}:
    TARGET_OWNER_NAMES = []
else:
    TARGET_OWNER_NAMES = parse_owner_names(TARGET_OWNER_NAMES_RAW) or ([TARGET_OWNER_NAME] if TARGET_OWNER_NAME else [])


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
    norm = normalize_text(stage_name)
    if not norm:
        return False
    return any(stage_matches(norm, target) for target in INCLUDED_STAGES)


def is_included_risk_label(risk_label: str) -> bool:
    if not INCLUDED_RISKS:
        return True
    norm = normalize_text(risk_label)
    if not norm:
        return False
    return any(stage_matches(norm, target) for target in INCLUDED_RISKS)


def get_portal_id() -> str:
    portal_id = os.getenv("HUBSPOT_PORTAL_ID", "").strip()
    if portal_id:
        return portal_id

    deals_url = os.getenv("HUBSPOT_DEALS_URL", "").strip()
    if deals_url:
        match = re.search(r"/contacts/(\d+)/", deals_url)
        if match:
            return match.group(1)

    raise RuntimeError(
        "Defina HUBSPOT_PORTAL_ID no .env (ex: HUBSPOT_PORTAL_ID=1234567) "
        "ou informe HUBSPOT_DEALS_URL contendo '/contacts/<portal_id>/...'."
    )


def canonical_deal_url(url: str, portal_id: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    clean = text.split("?", 1)[0].split("#", 1)[0]
    m = re.search(r"/(?:record|objects)/0-3/(?:record/)?(\d+)", clean)
    if m:
        return f"{HUBSPOT_BASE_URL}/contacts/{portal_id}/record/0-3/{m.group(1)}"
    return clean


def normalize_owner_name(value: str) -> str:
    return normalize_text(value)


def is_target_owner(owner_name: str) -> bool:
    if not TARGET_OWNER_NAMES:
        return True
    owner_norm = normalize_owner_name(owner_name)
    targets = [normalize_owner_name(x) for x in TARGET_OWNER_NAMES if x.strip()]
    if owner_norm in targets:
        return True

    owner_tokens = [t for t in owner_norm.split() if len(t) >= 3]
    for target in targets:
        target_tokens = [t for t in target.split() if len(t) >= 3]
        if not target_tokens:
            continue
        # Match tolerante para nomes exibidos com abreviacao/truncamento.
        matched = 0
        for tok in target_tokens:
            key = tok[:4]
            if any(ot.startswith(key) for ot in owner_tokens):
                matched += 1
        if matched >= max(2, len(target_tokens) // 2):
            return True
    return False


def is_excluded_stage(stage_name: str) -> bool:
    norm = normalize_text(stage_name)
    tokens = norm.split()
    has_neg = any(t.startswith("neg") for t in tokens)
    has_closed_lost = "closed" in tokens and "lost" in tokens
    has_lost = any(t.startswith("perdid") for t in tokens) or has_closed_lost
    return (has_neg and has_lost) or has_closed_lost


def is_closed_won_stage(stage_name: str) -> bool:
    norm = normalize_text(stage_name)
    tokens = norm.split()
    has_closed_won = "closed" in tokens and "won" in tokens
    has_closed_pt = any(t.startswith("fech") for t in tokens)
    has_lost = any(t.startswith("perdid") for t in tokens) or ("lost" in tokens)
    return (has_closed_pt and not has_lost) or has_closed_won


def deals_list_url(portal_id: str) -> str:
    custom_url = os.getenv("HUBSPOT_DEALS_URL", "").strip()
    if custom_url:
        return custom_url
    return f"{HUBSPOT_BASE_URL}/contacts/{portal_id}/objects/0-3/views/my/list"


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

    if env_flag("HUBSPOT_HEADLESS", default=False):
        raise RuntimeError(
            "Sessao invalida no HubSpot em modo headless. "
            "Atualize o arquivo hubspot-session.json com uma sessao valida antes de rodar no Docker."
        )

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


def refresh_hubspot_session_interactive(playwright, list_url: str, portal_id: str, storage_path: Path) -> None:
    browser = playwright.chromium.launch(**interactive_browser_launch_kwargs())
    context = browser.new_context()
    page = context.new_page()
    try:
        print("Abrindo HubSpot para renovacao manual da sessao...")
        ensure_authenticated(page, list_url, portal_id, storage_path)
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


def activate_list_filters(page, force_list_mode: bool = True) -> None:
    # Fecha overlays que podem bloquear clique.
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(600)

    # Aciona "Meus negócios" conforme solicitado.
    clicked_my_deals = bool(
        page.evaluate(
            """() => {
                const normalize = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();
                const nodes = Array.from(document.querySelectorAll("button, a, [role='tab'], [role='button']"));
                const candidate = nodes.find((n) => {
                    const txt = normalize(n.textContent || "");
                    if (!txt.includes("meus") || !txt.includes("negocios")) return false;
                    const rect = n.getBoundingClientRect();
                    return rect.width > 20 && rect.height > 20;
                });
                if (!candidate) return false;
                candidate.click();
                return true;
            }"""
        )
    )
    if clicked_my_deals:
        page.wait_for_timeout(2200)

    if force_list_mode:
        # Forca modo lista/tabela apenas quando a rotina depende disso.
        clicked_list_mode = bool(
            page.evaluate(
                """() => {
                    const normalize = (s) => (s || "")
                        .normalize("NFD")
                        .replace(/[\\u0300-\\u036f]/g, "")
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, " ")
                        .trim();
                    const buttons = Array.from(document.querySelectorAll("button, [role='button']"));
                    const candidate = buttons.find((b) => {
                        const attrs = [
                            b.getAttribute("aria-label") || "",
                            b.getAttribute("title") || "",
                            b.getAttribute("data-test-id") || "",
                            b.textContent || "",
                        ]
                            .map(normalize)
                            .join(" ");
                        return attrs.includes("lista")
                            || attrs.includes("tabela")
                            || attrs.includes("list")
                            || attrs.includes("table");
                    });
                    if (!candidate) return false;
                    candidate.click();
                    return true;
                }"""
            )
        )
        if clicked_list_mode:
            page.wait_for_timeout(1200)


def collect_deal_links(page, list_url: str, included_stages: list[str] | None = None):
    page.goto(list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)
    activate_list_filters(page)

    page.wait_for_timeout(1500)
    seen = set()
    max_pages = 120
    for _ in range(max_pages):
        page.wait_for_timeout(700)
        chunk = page.evaluate(
            """(includedStages) => {
                const patterns = ["/record/0-3/", "/objects/0-3/record/", "/deal/"];
                const normalize = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();
                const includes = (includedStages || []).map(normalize).filter(Boolean);
                const isLostStage = (text) => {
                    const tokens = normalize(text).split(" ").filter(Boolean);
                    const hasNeg = tokens.some(t => t.startsWith("neg"));
                    const hasLostPt = tokens.some(t => t.startsWith("perdid"));
                    const hasLostEn = tokens.includes("closed") && tokens.includes("lost");
                    return (hasNeg && hasLostPt) || hasLostEn;
                };
                const isIncludedStage = (text) => {
                    if (!includes.length) return true;
                    const stage = normalize(text);
                    if (!stage) return false;
                    const stageTokens = stage.split(" ").filter(t => t.length >= 3);
                    return includes.some((target) => {
                        if (stage.includes(target)) return true;
                        const targetTokens = target.split(" ").filter(t => t.length >= 3);
                        if (!targetTokens.length) return false;
                        return targetTokens.every(tok => {
                            const key = tok.slice(0, 4);
                            return stageTokens.some(s => s.startsWith(key));
                        });
                    });
                };

                const rows = Array.from(document.querySelectorAll("table tbody tr"));
                const seen = new Set();
                const found = [];
                for (const tr of rows) {
                    const linkEl = tr.querySelector("a[href*='/record/0-3/'], a[href*='/objects/0-3/record/']");
                    if (!linkEl) continue;
                    const href = (linkEl.href || "").split("?", 1)[0].split("#", 1)[0];
                    if (!href || !patterns.some(p => href.includes(p))) continue;

                    const stageEl = tr.querySelector("td[data-table-external-id*='dealstage'], td[data-table-external-id*='stage']");
                    const stageText = (stageEl && stageEl.innerText) || tr.textContent || "";
                    if (isLostStage(stageText)) continue;
                    if (!isIncludedStage(stageText)) continue;

                    if (!seen.has(href)) {
                        seen.add(href);
                        found.push(href);
                    }
                }
                return found;
            }""",
            included_stages or [],
        )
        for link in chunk or []:
            seen.add(link)

        has_next = bool(
            page.evaluate(
                """() => {
                    const next = document.querySelector("button[data-next-page='true']");
                    if (!next) return false;
                    const disabled = next.getAttribute("aria-disabled") === "true" || next.disabled;
                    if (disabled) return false;
                    next.click();
                    return true;
                }"""
            )
        )
        if not has_next:
            break
        page.wait_for_timeout(2200)

    links = sorted(seen)
    if not links:
        html_debug_path = Path("hubspot_deals_debug.html")
        html_debug_path.write_text(page.content(), encoding="utf-8")
        raise RuntimeError(
            "Nenhum link de negocio encontrado. Verifique o acesso ao pipeline e os seletores da pagina. "
            "HTML salvo em hubspot_deals_debug.html para depuracao."
        )
    return links


def collect_deal_links_from_board(
    page,
    list_url: str,
    included_stages: list[str] | None = None,
) -> list[str]:
    board_url = list_url.replace("/list", "/board")
    page.goto(board_url, wait_until="domcontentloaded")
    page.wait_for_timeout(7000)
    activate_list_filters(page, force_list_mode=False)
    page.wait_for_timeout(1200)
    # Garante varredura da esquerda para direita: comeca na primeira coluna.
    page.evaluate(
        """() => {
            const nodes = Array.from(document.querySelectorAll("*"));
            for (const el of nodes) {
                if (!el) continue;
                if ((el.scrollWidth - el.clientWidth) > 80) {
                    el.scrollLeft = 0;
                }
            }
            const root = document.scrollingElement || document.documentElement;
            if (root) root.scrollLeft = 0;
        }"""
    )
    page.wait_for_timeout(600)

    seen = set()
    stagnant_rounds = 0
    reached_closed_or_lost = False
    for _ in range(30):
        payload = page.evaluate(
            """(includedStages) => {
                const normalize = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();
                const includes = (includedStages || []).map(normalize).filter(Boolean);
                const isIncludedStage = (text) => {
                    if (!includes.length) return true;
                    const stage = normalize(text);
                    if (!stage) return false;
                    const stageTokens = stage.split(" ").filter(t => t.length >= 3);
                    return includes.some((target) => {
                        if (stage.includes(target)) return true;
                        const targetTokens = target.split(" ").filter(t => t.length >= 3);
                        if (!targetTokens.length) return false;
                        return targetTokens.every(tok => {
                            const key = tok.slice(0, 4);
                            return stageTokens.some(s => s.startsWith(key));
                        });
                    });
                };
                const isLostStage = (text) => {
                    const tokens = normalize(text).split(" ").filter(Boolean);
                    const hasNeg = tokens.some(t => t.startsWith("neg"));
                    const hasLostPt = tokens.some(t => t.startsWith("perdid"));
                    const hasLostEn = tokens.includes("closed") && tokens.includes("lost");
                    return (hasNeg && hasLostPt) || hasLostEn;
                };

                const columns = Array.from(document.querySelectorAll("[data-test-id^='framework-data-column-']"));
                const found = [];
                let reachedStopColumn = false;
                for (const col of columns) {
                    const stageName = (
                        col.querySelector("[data-test-id='cdb-column-name']")?.innerText
                        || col.getAttribute("data-column-id")
                        || ""
                    ).trim();
                    const columnId = (col.getAttribute("data-column-id") || "").toLowerCase();
                    if (isLostStage(stageName) || columnId.includes("closedlost")) {
                        reachedStopColumn = true;
                        break;
                    }
                    if (!isIncludedStage(stageName)) continue;

                    const scope = col.querySelector("[data-droppable-id]") || col;
                    const anchors = Array.from(scope.querySelectorAll("a[href]"));
                    for (const a of anchors) {
                        const href = (a.href || "").split("?", 1)[0].split("#", 1)[0];
                        if (!href) continue;
                        if (
                            href.includes("/record/0-3/")
                            || href.includes("/objects/0-3/record/")
                            || href.includes("/deal/")
                        ) {
                            found.push(href);
                        }
                    }

                    const html = scope.innerHTML || "";
                    const regex = /https?:\\/\\/app\\.hubspot\\.com\\/contacts\\/\\d+\\/(?:record\\/0-3\\/\\d+|objects\\/0-3\\/record\\/\\d+)/g;
                    const fromHtml = html.match(regex) || [];
                    for (const raw of fromHtml) {
                        const href = raw.split("?", 1)[0].split("#", 1)[0];
                        if (href) found.push(href);
                    }
                }
                return { links: [...new Set(found)], reachedStopColumn };
            }""",
            included_stages or [],
        )
        links = (payload or {}).get("links") or []
        reached_closed_or_lost = reached_closed_or_lost or bool((payload or {}).get("reachedStopColumn"))
        before = len(seen)
        for link in links or []:
            seen.add(link)

        movement = page.evaluate(
            """() => {
                const columns = Array.from(document.querySelectorAll("[data-test-id^='framework-data-column-']"));
                let boardScroller = null;
                for (const col of columns) {
                    let parent = col.parentElement;
                    while (parent) {
                        if ((parent.scrollWidth - parent.clientWidth) > 200) {
                            boardScroller = parent;
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    if (boardScroller) break;
                }

                if (!boardScroller) {
                    const candidates = Array.from(document.querySelectorAll("*"))
                        .filter((el) => (el.scrollWidth - el.clientWidth) > 200)
                        .sort((a, b) => (b.scrollWidth - b.clientWidth) - (a.scrollWidth - a.clientWidth));
                    boardScroller = candidates[0] || null;
                }

                let movedX = false;
                let atEndX = false;
                if (boardScroller) {
                    const maxX = Math.max(0, boardScroller.scrollWidth - boardScroller.clientWidth);
                    const stepX = Math.max(360, Math.floor(boardScroller.clientWidth * 0.9));
                    const nextX = Math.min(maxX, boardScroller.scrollLeft + stepX);
                    if (nextX > boardScroller.scrollLeft) {
                        boardScroller.scrollLeft = nextX;
                        movedX = true;
                    }
                    atEndX = maxX <= 0 || boardScroller.scrollLeft >= (maxX - 8);
                }

                let movedY = false;
                for (const col of columns) {
                    const scope = col.querySelector("[data-droppable-id]") || col;
                    const maxY = Math.max(0, scope.scrollHeight - scope.clientHeight);
                    if (maxY <= 0) continue;
                    const stepY = Math.max(220, Math.floor(scope.clientHeight * 0.85));
                    const nextY = Math.min(maxY, scope.scrollTop + stepY);
                    if (nextY > scope.scrollTop) {
                        scope.scrollTop = nextY;
                        movedY = true;
                    }
                }

                return { movedX, movedY, atEndX };
            }"""
        )
        page.wait_for_timeout(900)

        moved_x = bool((movement or {}).get("movedX"))
        at_end_x = bool((movement or {}).get("atEndX"))

        if len(seen) == before and not moved_x:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        if reached_closed_or_lost and at_end_x:
            break
        if stagnant_rounds >= 6 and at_end_x:
            break

    return sorted(seen)


def collect_stage_links_from_board(
    page,
    list_url: str,
    target_stages: list[str],
    max_rounds: int = 35,
) -> list[str]:
    board_url = list_url.replace("/list", "/board")
    page.goto(board_url, wait_until="domcontentloaded")
    page.wait_for_timeout(7000)
    activate_list_filters(page, force_list_mode=False)
    page.wait_for_timeout(1200)
    page.evaluate(
        """() => {
            const nodes = Array.from(document.querySelectorAll("*"));
            for (const el of nodes) {
                if (!el) continue;
                if ((el.scrollWidth - el.clientWidth) > 80) {
                    el.scrollLeft = 0;
                }
            }
            const root = document.scrollingElement || document.documentElement;
            if (root) root.scrollLeft = 0;
        }"""
    )
    page.wait_for_timeout(600)

    seen = set()
    stagnant_rounds = 0
    for _ in range(max_rounds):
        payload = page.evaluate(
            """(stageTargets) => {
                const normalize = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();
                const targets = (stageTargets || []).map(normalize).filter(Boolean);
                const isTargetStage = (text) => {
                    const stage = normalize(text);
                    if (!stage || !targets.length) return false;
                    const stageTokens = stage.split(" ").filter(t => t.length >= 3);
                    return targets.some((target) => {
                        if (stage.includes(target)) return true;
                        const targetTokens = target.split(" ").filter(t => t.length >= 3);
                        if (!targetTokens.length) return false;
                        return targetTokens.every(tok => {
                            const key = tok.slice(0, 4);
                            return stageTokens.some(s => s.startsWith(key));
                        });
                    });
                };

                const columns = Array.from(document.querySelectorAll("[data-test-id^='framework-data-column-']"));
                const found = [];
                for (const col of columns) {
                    const stageName = (
                        col.querySelector("[data-test-id='cdb-column-name']")?.innerText
                        || col.getAttribute("data-column-id")
                        || ""
                    ).trim();
                    if (!isTargetStage(stageName)) continue;

                    const scope = col.querySelector("[data-droppable-id]") || col;
                    const anchors = Array.from(scope.querySelectorAll("a[href]"));
                    for (const a of anchors) {
                        const href = (a.href || "").split("?", 1)[0].split("#", 1)[0];
                        if (!href) continue;
                        if (
                            href.includes("/record/0-3/")
                            || href.includes("/objects/0-3/record/")
                            || href.includes("/deal/")
                        ) {
                            found.push(href);
                        }
                    }

                    const html = scope.innerHTML || "";
                    const regex = /https?:\\/\\/app\\.hubspot\\.com\\/contacts\\/\\d+\\/(?:record\\/0-3\\/\\d+|objects\\/0-3\\/record\\/\\d+)/g;
                    const fromHtml = html.match(regex) || [];
                    for (const raw of fromHtml) {
                        const href = raw.split("?", 1)[0].split("#", 1)[0];
                        if (href) found.push(href);
                    }
                }

                let boardScroller = null;
                for (const col of columns) {
                    let parent = col.parentElement;
                    while (parent) {
                        if ((parent.scrollWidth - parent.clientWidth) > 200) {
                            boardScroller = parent;
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    if (boardScroller) break;
                }
                if (!boardScroller) {
                    const candidates = Array.from(document.querySelectorAll("*"))
                        .filter((el) => (el.scrollWidth - el.clientWidth) > 200)
                        .sort((a, b) => (b.scrollWidth - b.clientWidth) - (a.scrollWidth - a.clientWidth));
                    boardScroller = candidates[0] || null;
                }

                let movedX = false;
                let atEndX = false;
                if (boardScroller) {
                    const maxX = Math.max(0, boardScroller.scrollWidth - boardScroller.clientWidth);
                    const stepX = Math.max(360, Math.floor(boardScroller.clientWidth * 0.9));
                    const nextX = Math.min(maxX, boardScroller.scrollLeft + stepX);
                    if (nextX > boardScroller.scrollLeft) {
                        boardScroller.scrollLeft = nextX;
                        movedX = true;
                    }
                    atEndX = maxX <= 0 || boardScroller.scrollLeft >= (maxX - 8);
                }

                return { links: [...new Set(found)], movedX, atEndX };
            }""",
            target_stages,
        )
        links = (payload or {}).get("links") or []
        before = len(seen)
        for link in links:
            seen.add(link)
        page.wait_for_timeout(900)

        moved_x = bool((payload or {}).get("movedX"))
        at_end_x = bool((payload or {}).get("atEndX"))
        if len(seen) == before and not moved_x:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        if at_end_x and stagnant_rounds >= 4:
            break

    return sorted(seen)


def collect_deals_from_list_table(
    page,
    list_url: str,
    target_owners: list[str] | None = None,
    included_stages: list[str] | None = None,
    max_pages: int = 15,
):
    page.goto(list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    activate_list_filters(page)
    page.wait_for_timeout(1200)

    results = []
    seen = set()
    for _ in range(max_pages):
        rows = page.evaluate(
            """(payload) => {
                const normalize = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();
                const ownerTargets = ((payload && payload.ownerNames) || []).map(normalize).filter(Boolean);
                const ownerKeys = ownerTargets.map(t => t.split(" ").slice(0, 2).join(" "));
                const includes = ((payload && payload.includedStages) || []).map(normalize).filter(Boolean);
                const isLostStage = (text) => {
                    const tokens = normalize(text).split(" ").filter(Boolean);
                    const hasNeg = tokens.some(t => t.startsWith("neg"));
                    const hasLostPt = tokens.some(t => t.startsWith("perdid"));
                    const hasLostEn = tokens.includes("closed") && tokens.includes("lost");
                    return (hasNeg && hasLostPt) || hasLostEn;
                };
                const isIncludedStage = (text) => {
                    if (!includes.length) return true;
                    const stage = normalize(text);
                    if (!stage) return false;
                    const stageTokens = stage.split(" ").filter(t => t.length >= 3);
                    return includes.some((target) => {
                        if (stage.includes(target)) return true;
                        const targetTokens = target.split(" ").filter(t => t.length >= 3);
                        if (!targetTokens.length) return false;
                        return targetTokens.every(tok => {
                            const key = tok.slice(0, 4);
                            return stageTokens.some(s => s.startsWith(key));
                        });
                    });
                };
                const rowNodes = Array.from(document.querySelectorAll("table tbody tr"));
                const items = [];
                for (const tr of rowNodes) {
                    const linkEl = tr.querySelector("a[href*='/record/0-3/'], a[href*='/objects/0-3/record/']");
                    if (!linkEl) continue;
                    const href = (linkEl.href || "").split("?", 1)[0].split("#", 1)[0];
                    if (!href) continue;

                    const stageEl = tr.querySelector("td[data-table-external-id*='dealstage'], td[data-table-external-id*='stage']");
                    const ownerEl = tr.querySelector("td[data-table-external-id*='hubspot_owner_id'], td[data-table-external-id*='owner']");
                    const amountEl = tr.querySelector("td[data-table-external-id*='amount']");
                    const companyEl = tr.querySelector("td[data-table-external-id*='associatedcompany'], td[data-table-external-id*='company']");
                    const stage = (stageEl?.innerText || tr.innerText || "").trim();
                    const owner = (ownerEl?.innerText || "").trim();
                    const value = (amountEl?.innerText || "").trim();
                    const company = (companyEl?.innerText || "").trim();
                    const name = (linkEl.innerText || linkEl.textContent || "").trim();
                    const rowText = normalize(tr.textContent || "");
                    if (isLostStage(stage || rowText)) continue;
                    const ownerMatch = !ownerTargets.length || ownerTargets.some((target, idx) => {
                        const key = ownerKeys[idx];
                        return rowText.includes(target) || (key && rowText.includes(key));
                    });
                    if (!ownerMatch) continue;
                    if (!isIncludedStage(stage || rowText)) continue;
                    items.push({ url: href, nome: name, empresa: company, etapa: stage, valor: value, proprietario: owner });
                }
                return items;
            }""",
            {
                "ownerNames": target_owners or [],
                "includedStages": included_stages or [],
            },
        )

        for row in rows or []:
            url = (row or {}).get("url", "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(row)

        has_next = bool(
            page.evaluate(
                """() => {
                    const next = document.querySelector("button[data-next-page='true']");
                    if (!next) return false;
                    const disabled = next.getAttribute("aria-disabled") === "true" || next.disabled;
                    if (disabled) return false;
                    next.click();
                    return true;
                }"""
            )
        )
        if not has_next:
            break
        page.wait_for_timeout(1800)

    normalized = []
    for row in results:
        item = {
            "url": canonical_deal_url((row.get("url") or "").strip(), get_portal_id()),
            "nome": (row.get("nome") or "").strip() or "Sem nome",
            "empresa": (row.get("empresa") or "").strip() or infer_company_from_deal_name((row.get("nome") or "").strip()),
            "etapa": (row.get("etapa") or "").strip() or "Sem etapa",
            "valor": (row.get("valor") or "").strip() or "--",
            "proprietario": (row.get("proprietario") or "").strip() or (TARGET_OWNER_NAMES[0] if TARGET_OWNER_NAMES else ""),
            "ultima_atividade": "",
            "ultimas_interacoes": {
                "lista": [],
                "por_aba": {},
                "ultimas_por_tipo": {
                    "atividade": "",
                    "observacao": "",
                    "email": "",
                    "tarefa": "",
                    "reuniao": "",
                },
            },
        }
        if not is_included_stage(item.get("etapa", "")):
            continue
        if is_excluded_stage(item.get("etapa", "")):
            continue
        if not is_included_risk_for_deal(item):
            continue
        item["resumo"] = build_summary(item)
        normalized.append(item)
    return normalized


def register_seed_rows(seed_rows_by_url: dict[str, dict], rows: list[dict], portal_id: str) -> None:
    for row in rows or []:
        url = canonical_deal_url((row.get("url") or "").strip(), portal_id)
        if not url:
            continue
        current = seed_rows_by_url.get(url, {})
        merged = {
            **current,
            "url": url,
            "nome": (row.get("nome") or current.get("nome") or "").strip(),
            "empresa": (row.get("empresa") or current.get("empresa") or "").strip(),
            "etapa": (row.get("etapa") or current.get("etapa") or "").strip(),
            "valor": (row.get("valor") or current.get("valor") or "").strip(),
            "proprietario": (row.get("proprietario") or current.get("proprietario") or "").strip(),
        }
        seed_rows_by_url[url] = merged


def merge_seed_metadata(data: dict, seed: dict | None) -> dict:
    if not seed:
        return data

    merged = dict(data)
    for field in ("nome", "empresa", "valor"):
        if not (merged.get(field) or "").strip() and (seed.get(field) or "").strip():
            merged[field] = (seed.get(field) or "").strip()

    seed_stage = (seed.get("etapa") or "").strip()
    if seed_stage:
        current_stage = (merged.get("etapa") or "").strip()
        if (not current_stage) or (not is_included_stage(current_stage) and is_included_stage(seed_stage)):
            merged["etapa"] = seed_stage

    seed_owner = (seed.get("proprietario") or "").strip()
    if seed_owner:
        current_owner = (merged.get("proprietario") or "").strip()
        if (not current_owner) or (not is_target_owner(current_owner) and is_target_owner(seed_owner)):
            merged["proprietario"] = seed_owner

    return merged


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


def classify_interaction(text: str, event_type: str = "") -> str:
    raw_event_type = (event_type or "").strip().lower()
    if raw_event_type:
        if "note" in raw_event_type:
            return "observacao"
        if "email" in raw_event_type:
            return "email"
        if "task" in raw_event_type:
            return "tarefa"
        if "meeting" in raw_event_type:
            return "reuniao"

    low = normalize_text(text)
    if "tarefa" in low:
        return "tarefa"
    if "reuni" in low or "meeting" in low:
        return "reuniao"
    if "e mail" in low or "email" in low:
        return "email"
    if "observa" in low or "nota" in low:
        return "observacao"
    return "atividade"


def infer_company_from_deal_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    parts = [part.strip() for part in re.findall(r"\[([^\]]+)\]", text) if part.strip()]
    if parts:
        return parts[0]
    if " - " in text:
        return text.split(" - ", 1)[0].strip()
    return ""


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


def wait_for_activity_timeline(page, timeout_ms: int = 12000) -> None:
    try:
        page.wait_for_function(
            """() => {
                const norm = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();
                const hasTimeline = Boolean(
                    document.querySelector("[data-test-id='crm-events-viz-timeline']")
                    || document.querySelector("[data-selenium-test='timeline-card']")
                );
                if (hasTimeline) return true;
                const tabs = Array.from(document.querySelectorAll("button, a, [role='tab']"));
                return tabs.some((node) => {
                    const txt = norm(node.textContent || "");
                    return txt.includes("atividade")
                        || txt.includes("observa")
                        || txt.includes("email")
                        || txt.includes("reuniao")
                        || txt.includes("tarefa");
                });
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        page.wait_for_timeout(800)


def switch_activity_tab(page, tab_key: str) -> bool:
    aliases = {
        "atividade": ["atividade"],
        "observacao": ["observacoes", "observacao", "notas", "nota"],
        "email": ["emails", "email", "emails"],
        "tarefa": ["tarefas", "tarefa"],
        "reuniao": ["reunioes", "reuniao"],
    }
    possible = aliases.get(tab_key, [tab_key])
    clicked = bool(
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
                const found = nodes.find((n) => {
                    const txt = norm(n.textContent);
                    if (!targets.some((t) => txt.includes(t))) return false;
                    const rect = n.getBoundingClientRect();
                    return rect.width > 12 && rect.height > 12;
                });
                if (!found) return false;
                found.scrollIntoView({ block: "center", inline: "center" });
                found.click();
                return true;
            }""",
            possible,
        )
    )
    if not clicked:
        return False
    try:
        page.wait_for_function(
            """(names) => {
                const norm = (s) => (s || "")
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[-\\s]/g, "");
                const targets = names.map(norm);
                const scope = document.querySelector("[data-test-id='crm-events-viz-timeline']") || document;
                const nodes = Array.from(scope.querySelectorAll("button, a, [role='tab']"));
                const found = nodes.find((n) => {
                    const txt = norm(n.textContent);
                    return targets.some((t) => txt.includes(t));
                });
                if (!found) return false;
                const attrs = [
                    found.getAttribute("aria-selected") || "",
                    found.getAttribute("aria-pressed") || "",
                    found.getAttribute("aria-current") || "",
                    found.getAttribute("data-selected") || "",
                ]
                    .map((value) => value.toLowerCase());
                const className = (found.className || "").toString().toLowerCase();
                return attrs.includes("true")
                    || attrs.includes("page")
                    || className.includes("selected")
                    || className.includes("active");
            }""",
            possible,
            timeout=2500,
        )
    except Exception:
        page.wait_for_timeout(700)
    return True


def reset_timeline_scroll(page) -> None:
    page.evaluate(
        """() => {
            const scope = document.querySelector("[data-test-id='crm-events-viz-timeline']") || document;
            const nodes = [scope, ...Array.from(scope.querySelectorAll("*"))];
            for (const node of nodes) {
                if (!node) continue;
                if ((node.scrollHeight - node.clientHeight) > 120) {
                    node.scrollTop = 0;
                }
            }
            const root = document.scrollingElement || document.documentElement;
            if (root) root.scrollTop = 0;
        }"""
    )
    page.wait_for_timeout(500)


def scroll_timeline(page, delta: int) -> None:
    page.evaluate(
        """(delta) => {
            const scope = document.querySelector("[data-test-id='crm-events-viz-timeline']") || document;
            const nodes = [scope, ...Array.from(scope.querySelectorAll("*"))];
            const scrollables = nodes.filter((node) => node && (node.scrollHeight - node.clientHeight) > 120);
            const target = scrollables.sort((a, b) => (b.clientHeight - a.clientHeight))[0];
            if (target) {
                target.scrollTop += delta;
                return;
            }
            const root = document.scrollingElement || document.documentElement;
            if (root) root.scrollTop += delta;
        }""",
        delta,
    )
    page.wait_for_timeout(900)


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


def expand_timeline_cards(page, limit: int = 12) -> None:
    page.evaluate(
        """(limit) => {
            const cards = Array.from(document.querySelectorAll("[data-selenium-test='timeline-card']"));
            let expanded = 0;
            for (const card of cards) {
                if (expanded >= limit) break;
                const toggle = card.querySelector("[data-test-id='collapsible-event-accordion'] [role='button'][aria-expanded='false']");
                    // Fallback para cards sem wrapper padrao.
                const candidate = toggle || card.querySelector("[role='button'][aria-expanded='false']");
                if (!candidate) continue;
                candidate.click();
                expanded += 1;
            }
        }""",
        limit,
    )
    page.wait_for_timeout(1200)


def extract_timeline_events(page, limit: int = 5):
    return page.evaluate(
        """(limit) => {
            const cards = Array.from(document.querySelectorAll("[data-selenium-test='timeline-card']"));
            return cards.slice(0, limit).map((card) => {
                const header = card.querySelector("[data-test-id='header-message']")?.innerText || "";
                const timestamp = card.querySelector("[data-test-id='event-timestamp']")?.innerText || "";
                const eventType = card.getAttribute("data-selenium-event-type") || card.parentElement?.getAttribute("data-event-type") || "";
                const text = (card.innerText || card.textContent || "").replace(/\\s+/g, " ").trim();
                return { text, header, timestamp, eventType };
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
    for pat in relation_prefixes:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text).strip(" .|-")
    if re.fullmatch(r"https?://\S+", text, flags=re.IGNORECASE):
        return ""

    if tab == "email":
        return text[:4000]
    return text[:3000]


def has_any_interactions(interactions: dict | None) -> bool:
    payload = interactions or {}
    by_tab = payload.get("por_aba") or {}
    for values in by_tab.values():
        if isinstance(values, list) and any((item or "").strip() for item in values):
            return True
    latest = payload.get("ultimas_por_tipo") or {}
    return any((value or "").strip() for value in latest.values())


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
        "adicionar descrição",
        "adicionar descricao",
        "acoes",
        "atividade",
    }
    if cleaned in blocked or normalize_text(cleaned) in {normalize_text(item) for item in blocked}:
        return False
    if (
        (cleaned.startswith("observação de ") or cleaned.startswith("observacao de "))
        and len(cleaned.split()) <= 5
    ):
        return False
    return True


def sanitize_interactions_payload(payload: dict | None, limit: int = 5) -> dict:
    tabs = ["atividade", "observacao", "email", "tarefa", "reuniao"]
    by_tab_raw = (payload or {}).get("por_aba") or {}
    by_tab = {tab: [] for tab in tabs}

    for tab in tabs:
        seen = set()
        for raw_text in by_tab_raw.get(tab) or []:
            text = " ".join((raw_text or "").split()).strip()
            if not is_meaningful_interaction_text(text):
                continue
            key = normalize_text(text)
            if not key or key in seen:
                continue
            seen.add(key)
            by_tab[tab].append(text)
            if len(by_tab[tab]) >= limit:
                break

    merged = []
    seen_merged = set()
    for tab in tabs:
        for text in by_tab.get(tab, []):
            key = f"{tab}:{normalize_text(text)}"
            if key in seen_merged:
                continue
            seen_merged.add(key)
            merged.append({"tipo": tab, "texto": text})

    latest_by_type = {
        tab: (by_tab.get(tab) or [""])[0] if by_tab.get(tab) else ""
        for tab in tabs
    }
    if not latest_by_type["atividade"]:
        latest_by_type["atividade"] = next(
            (item["texto"] for item in merged if (item.get("texto") or "").strip()),
            "",
        )

    return {
        "lista": merged,
        "por_aba": by_tab,
        "ultimas_por_tipo": latest_by_type,
    }


def normalize_deal_payload(data: dict | None) -> dict | None:
    if not data:
        return None

    normalized = dict(data)
    for field in ("nome", "empresa", "etapa", "valor", "proprietario", "ultima_atividade", "url"):
        normalized[field] = " ".join((normalized.get(field) or "").split()).strip()

    if not normalized.get("empresa"):
        normalized["empresa"] = infer_company_from_deal_name(normalized.get("nome", ""))

    normalized["ultimas_interacoes"] = sanitize_interactions_payload(
        normalized.get("ultimas_interacoes"),
        limit=10,
    )

    if not normalized.get("nome"):
        return None
    return normalized


def has_summary_source_content(by_tab: dict) -> bool:
    return bool((by_tab.get("observacao") or []) or (by_tab.get("reuniao") or []))


def has_observation_content(by_tab: dict) -> bool:
    return bool(by_tab.get("observacao") or [])


def collect_recent_interactions(page, limit: int = 5):
    tabs = ["atividade", "observacao", "email", "tarefa", "reuniao"]
    last_payload = {
        "lista": [],
        "por_aba": {tab: [] for tab in tabs},
        "ultimas_por_tipo": {tab: "" for tab in tabs},
    }

    for attempt in range(INTERACTION_COLLECTION_ATTEMPTS):
        if attempt == 0:
            page.wait_for_timeout(TIMELINE_INITIAL_WAIT_MS)
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(TIMELINE_TAB_WAIT_MS)
        else:
            print("Aviso: observacoes/reunioes nao carregaram de forma confiavel. Repetindo coleta da timeline...")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(TIMELINE_TAB_WAIT_MS)
            page.mouse.wheel(0, -1200)
            page.wait_for_timeout(TIMELINE_TAB_WAIT_MS)
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(TIMELINE_EMPTY_RETRY_WAIT_MS)

        wait_for_activity_timeline(page, timeout_ms=16000 if attempt == 0 else 20000)
        ensure_activities_view(page)

        by_tab = {tab: [] for tab in tabs}
        seen_by_tab = {tab: set() for tab in tabs}

        def add_event_to_tabs(event: dict, current_tab: str) -> None:
            raw_text = " ".join(
                part.strip()
                for part in (
                    event.get("header", "") or "",
                    event.get("timestamp", "") or "",
                    event.get("text", "") or "",
                )
                if (part or "").strip()
            )
            classified_type = classify_interaction(raw_text, event.get("eventType", ""))
            if current_tab == "atividade":
                target_tab = classified_type if classified_type in tabs and classified_type != "atividade" else "atividade"
            else:
                target_tab = current_tab
            event_text = clean_event_text(raw_text, target_tab)
            if not event_text or not is_meaningful_interaction_text(event_text):
                return

            activity_key = normalize_text(event_text)
            if activity_key and activity_key not in seen_by_tab["atividade"]:
                seen_by_tab["atividade"].add(activity_key)
                by_tab["atividade"].append(event_text)

            if target_tab != "atividade" and activity_key and activity_key not in seen_by_tab[target_tab]:
                seen_by_tab[target_tab].add(activity_key)
                by_tab[target_tab].append(event_text)

        def capture_visible_tab_events(current_tab: str, rounds: int) -> list[dict]:
            captured = []
            seen_event_keys = set()
            for round_idx in range(rounds):
                expand_all_activities(page)
                expand_timeline_cards(page, limit=max(limit * 2, 16))
                events = extract_timeline_events(page, limit=max(limit * 14, 80))
                for event in events:
                    raw_key = normalize_text(" ".join((event.get("eventType", ""), event.get("text", ""))))
                    if not raw_key or raw_key in seen_event_keys:
                        continue
                    seen_event_keys.add(raw_key)
                    captured.append(event)
                if round_idx < rounds - 1:
                    scroll_timeline(page, 2200)
            for event in captured:
                add_event_to_tabs(event, current_tab)
            return captured

        for tab in tabs:
            if tab == "atividade":
                ensure_activities_view(page)
            elif not switch_activity_tab(page, tab):
                print(f"Aviso: nao foi possivel abrir a aba de {tab} neste negocio.")
                continue

            reset_timeline_scroll(page)
            page.wait_for_timeout(TIMELINE_TAB_WAIT_MS)
            rounds = 3 if tab == "atividade" else 2
            capture_visible_tab_events(tab, rounds)

            if tab in {"observacao", "reuniao"} and not by_tab[tab]:
                print(f"Aviso: aba de {tab} veio vazia. Tentando recaptura apos nova espera...")
                for retry_wait in (TIMELINE_EMPTY_RETRY_WAIT_MS, TIMELINE_EMPTY_RETRY_WAIT_MS * 2):
                    page.wait_for_timeout(retry_wait)
                    if switch_activity_tab(page, tab):
                        reset_timeline_scroll(page)
                        page.wait_for_timeout(TIMELINE_TAB_WAIT_MS)
                        capture_visible_tab_events(tab, 3)
                    if by_tab[tab]:
                        break

        payload = sanitize_interactions_payload({"por_aba": by_tab}, limit=limit)
        last_payload = payload

        if has_observation_content(payload.get("por_aba") or {}) or attempt == INTERACTION_COLLECTION_ATTEMPTS - 1:
            return payload
        if has_summary_source_content(payload.get("por_aba") or {}):
            print("Aviso: reuniao encontrada, mas observacao ainda vazia. Aguardando nova tentativa para evitar falso vazio.")

    return last_payload


def collect_deal_details(page, url: str):
    last_payload = None
    for attempt in range(2):
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000 if attempt == 0 else 4500)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        wait_for_activity_timeline(page, timeout_ms=12000 if attempt == 0 else 16000)

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
        if not company:
            company = infer_company_from_deal_name(name)

        interactions = collect_recent_interactions(page, limit=10)
        payload = {
            "url": url,
            "nome": name,
            "empresa": company,
            "etapa": stage,
            "valor": value,
            "proprietario": owner,
            "ultima_atividade": last_activity,
            "ultimas_interacoes": interactions,
        }
        payload = normalize_deal_payload(payload) or payload
        last_payload = payload

        if has_any_interactions(interactions) or attempt == 1:
            return payload

        print(f"Aviso: negocio aberto sem interacoes detectadas. Recarregando coleta detalhada: {url}")

    return last_payload or {
        "url": url,
        "nome": "",
        "empresa": "",
        "etapa": "",
        "valor": "",
        "proprietario": "",
        "ultima_atividade": "",
        "ultimas_interacoes": {"lista": [], "por_aba": {}, "ultimas_por_tipo": {}},
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


def deal_risk_label(item: dict) -> str:
    last_activity_date = parse_activity_date(item.get("ultima_atividade", ""))
    days_without = None
    if last_activity_date:
        days_without = (datetime.now().date() - last_activity_date).days
    return risk_status(days_without)


def is_included_risk_for_deal(item: dict) -> bool:
    return is_included_risk_label(deal_risk_label(item))


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


def open_hubspot_session(playwright, list_url: str, portal_id: str):
    browser = playwright.chromium.launch(**browser_launch_kwargs())
    context_kwargs = {}
    if STORAGE_STATE_PATH.exists():
        context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    ensure_authenticated(page, list_url, portal_id, STORAGE_STATE_PATH)
    return browser, context, page


def collect_board_links_in_isolated_context(playwright, list_url: str, included_stages: list[str] | None = None) -> list[str]:
    if not STORAGE_STATE_PATH.exists():
        return []

    browser = playwright.chromium.launch(**browser_launch_kwargs())
    context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
    page = context.new_page()
    try:
        return collect_deal_links_from_board(page, list_url, included_stages)
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh-session",
        action="store_true",
        help="Abre o navegador em modo visivel para renovar o arquivo hubspot-session.json.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    portal_id = get_portal_id()
    list_url = deals_list_url(portal_id)

    with sync_playwright() as p:
        if args.refresh_session:
            refresh_hubspot_session_interactive(p, list_url, portal_id, STORAGE_STATE_PATH)
            print(f"Sessao atualizada com sucesso em: {STORAGE_STATE_PATH.resolve()}")
            return

        browser = None
        context = None
        page = None
        seed_rows_by_url = {}
        try:
            browser, context, page = open_hubspot_session(p, list_url, portal_id)
            links = []

            # Coleta o board em um contexto isolado porque a navegacao previa em /list
            # altera o estado visual e pode esconder a coluna "Negocio Fechado".
            try:
                links = collect_board_links_in_isolated_context(p, list_url, INCLUDED_STAGES)
                if links:
                    print(f"Links capturados no board isolado: {len(links)}")
            except Exception as exc:
                print(f"Aviso: falha ao coletar links no board isolado: {exc}")

            closed_stage_targets = [stage for stage in (INCLUDED_STAGES_RAW.split(",") if INCLUDED_STAGES_RAW else []) if is_closed_won_stage(stage)]
            if closed_stage_targets:
                try:
                    closed_links = collect_board_links_in_isolated_context(p, list_url, closed_stage_targets)
                    if closed_links:
                        print(f"Links capturados especificamente em Negocio Fechado: {len(closed_links)}")
                    links = sorted(set(links).union(closed_links))
                except Exception as exc:
                    print(f"Aviso: falha ao coletar links da coluna Negocio Fechado: {exc}")

            # Complemento pela tabela/lista filtrada para capturar cards que nao estiverem montados no board.
            try:
                table_rows = collect_deals_from_list_table(
                    page,
                    list_url,
                    TARGET_OWNER_NAMES,
                    INCLUDED_STAGES,
                    max_pages=15,
                )
                register_seed_rows(seed_rows_by_url, table_rows, portal_id)
                table_urls = {
                    (r.get("url") or "").strip()
                    for r in table_rows
                    if (r.get("url") or "").strip()
                }
                links = sorted(set(links).union(table_urls))
            except Exception as exc:
                print(f"Aviso: falha ao coletar links pela tabela: {exc}")

            if closed_stage_targets:
                try:
                    closed_table_rows = collect_deals_from_list_table(
                        page,
                        list_url,
                        None,
                        closed_stage_targets,
                        max_pages=25,
                    )
                    register_seed_rows(seed_rows_by_url, closed_table_rows, portal_id)
                    closed_table_urls = {
                        (r.get("url") or "").strip()
                        for r in closed_table_rows
                        if (r.get("url") or "").strip()
                    }
                    if closed_table_urls:
                        print(f"Links capturados pela tabela para Negocio Fechado: {len(closed_table_urls)}")
                    links = sorted(set(links).union(closed_table_urls))
                except Exception as exc:
                    print(f"Aviso: falha ao coletar Negocio Fechado pela tabela: {exc}")

            # Fallback 2: varredura legacy em lista paginada.
            if not links:
                links = collect_deal_links(page, list_url, INCLUDED_STAGES)
            owner_scope = ", ".join(TARGET_OWNER_NAMES) if TARGET_OWNER_NAMES else "todos os responsaveis"
            risk_scope = INCLUDED_RISKS_RAW if INCLUDED_RISKS_RAW else "todos os riscos"
            print(
                f"Links elegiveis por etapa ({INCLUDED_STAGES_RAW}), responsavel ({owner_scope}) e risco ({risk_scope}): {len(links)}"
            )
            links = sorted({canonical_deal_url(link, portal_id) for link in links if canonical_deal_url(link, portal_id)})
            print(f"Links unicos apos normalizacao por ID: {len(links)}")
        except PlaywrightTimeoutError as exc:
            if browser:
                browser.close()
            raise RuntimeError("Timeout ao carregar paginas do HubSpot.") from exc

        results = []
        skipped_lost_stage = 0
        for link in links:
            collected = False
            for attempt in range(3):
                try:
                    data = collect_deal_details(page, link)
                    data = merge_seed_metadata(data, seed_rows_by_url.get(link))
                    data = normalize_deal_payload(data)
                    if not data:
                        print(f"Aviso: negocio ignorado por falta de nome apos coleta: {link}")
                        collected = True
                        break
                    if not (data.get("proprietario") or "").strip() and TARGET_OWNER_NAMES:
                        data["proprietario"] = TARGET_OWNER_NAMES[0]
                    if not is_target_owner(data.get("proprietario", "")):
                        collected = True
                        break
                    if not is_included_stage(data.get("etapa", "")):
                        collected = True
                        break
                    if is_excluded_stage(data.get("etapa", "")):
                        skipped_lost_stage += 1
                        collected = True
                        break
                    if not is_included_risk_for_deal(data):
                        collected = True
                        break
                    data["resumo"] = build_summary(data)
                    results.append(data)
                    if len(results) >= MAX_TARGET_DEALS:
                        break
                    print("=" * 80)
                    print(data["resumo"])
                    collected = True
                    break
                except Exception as exc:
                    msg = str(exc).lower()
                    session_closed = "target page, context or browser has been closed" in msg or "has been closed" in msg
                    if session_closed and attempt < 2:
                        print("Aba/sessao fechada durante coleta. Reiniciando navegador e retomando...")
                        try:
                            if context:
                                context.close()
                        except Exception:
                            pass
                        try:
                            if browser:
                                browser.close()
                        except Exception:
                            pass
                        browser, context, page = open_hubspot_session(p, list_url, portal_id)
                        continue
                    print(f"Erro ao processar negocio {link}: {exc}")
                    break
            if len(results) >= MAX_TARGET_DEALS:
                break
            if not collected:
                continue

        if skipped_lost_stage:
            print(f"Negocios ignorados por etapa perdida: {skipped_lost_stage}")

        result_urls = {(item.get("url") or "").strip() for item in results if (item.get("url") or "").strip()}
        for url, seed in seed_rows_by_url.items():
            if url in result_urls:
                continue
            if not is_closed_won_stage(seed.get("etapa", "")):
                continue
            if TARGET_OWNER_NAMES and not is_target_owner(seed.get("proprietario", "")):
                continue
            fallback_item = {
                "url": url,
                "nome": seed.get("nome", "") or "Sem nome",
                "empresa": seed.get("empresa", ""),
                "etapa": seed.get("etapa", ""),
                "valor": seed.get("valor", "") or "--",
                "proprietario": seed.get("proprietario", "") or (TARGET_OWNER_NAMES[0] if TARGET_OWNER_NAMES else ""),
                "ultima_atividade": "",
                "ultimas_interacoes": {
                    "lista": [],
                    "por_aba": {},
                    "ultimas_por_tipo": {
                        "atividade": "",
                        "observacao": "",
                        "email": "",
                        "tarefa": "",
                        "reuniao": "",
                    },
                },
            }
            fallback_item = normalize_deal_payload(fallback_item)
            if not fallback_item:
                continue
            if not is_included_stage(fallback_item.get("etapa", "")):
                continue
            if is_excluded_stage(fallback_item.get("etapa", "")):
                continue
            if not is_included_risk_for_deal(fallback_item):
                continue
            fallback_item["resumo"] = build_summary(fallback_item)
            results.append(fallback_item)
            print(f"Aviso: negocio fechado incluido via fallback da tabela: {fallback_item['nome']}")

        if not results:
            print("Coleta detalhada retornou 0 negocios. Tentando fallback pela tabela em modo lista...")
            try:
                results = collect_deals_from_list_table(page, list_url, TARGET_OWNER_NAMES, INCLUDED_STAGES)
            except Exception as exc:
                msg = str(exc).lower()
                if "has been closed" in msg:
                    print("Sessao fechada durante fallback. Reiniciando navegador para tentar novamente...")
                    browser, context, page = open_hubspot_session(p, list_url, portal_id)
                    try:
                        results = collect_deals_from_list_table(page, list_url, TARGET_OWNER_NAMES, INCLUDED_STAGES)
                    except Exception as exc2:
                        print(f"Fallback pela tabela falhou apos reinicio: {exc2}")
                else:
                    print(f"Fallback pela tabela falhou: {exc}")

        previous_items = []
        if OUTPUT_PATH.exists():
            try:
                previous_items = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            except Exception:
                previous_items = []

        if results:
            with OUTPUT_PATH.open("w", encoding="utf-8") as fp:
                json.dump(results, fp, ensure_ascii=False, indent=2)
            weekly_report = build_weekly_report(results)
            WEEKLY_REPORT_PATH.write_text(weekly_report, encoding="utf-8")
            print(f"Arquivo gerado: {OUTPUT_PATH}")
            print(f"Arquivo gerado: {WEEKLY_REPORT_PATH}")
        elif previous_items:
            print(
                "Nenhum negocio elegivel foi coletado nesta execucao. "
                f"Mantendo arquivo anterior com {len(previous_items)} negocio(s)."
            )
            WEEKLY_REPORT_PATH.write_text(build_weekly_report(previous_items), encoding="utf-8")
        else:
            raise RuntimeError(
                "Nenhum negocio elegivel foi coletado. Verifique sessao/filtros no HubSpot e rode novamente."
            )

        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
