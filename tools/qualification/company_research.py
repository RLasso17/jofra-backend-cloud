# tools/qualification/company_research.py
"""
Investigacion profunda de empresas para el Agente 2 (Qualifier).

deep_research_company() junta evidencia REAL de la web (busquedas + la
propia pagina de la empresa) y la consolida en un reporte de texto que el
LLM (ministral-3:8b) evaluara para decidir si la empresa encaja en el
perfil de cliente de Jofra:

SI encaja  -> instalaciones propias, naves industriales, plantas de
              produccion, operacion 24/7, camaras de refrigeracion,
              alto consumo electrico (tarifas GDMTO/GDMTH/PDBT).
NO encaja  -> coworking, oficinas rentadas, plazas comerciales, edificios
              corporativos verticales, empresas muy pequenas.

Esta tool NO toma la decision final: recolecta y estructura evidencia con
sus fuentes. Decidir es trabajo del LLM del Agente 2.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from tools.scraping.anti_ban import build_browser_headers, jitter_sleep
from tools.scraping.google_dorking import web_search
from tools.scraping.website_extractor import fetch_html

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_ABOUT_PAGES = 3
EXCERPT_RADIUS = 90  # caracteres de contexto alrededor de cada hallazgo
MAX_PAGE_TEXT = 20000

# Senales POSITIVAS: la empresa tiene instalaciones propias con consumo alto.
POSITIVE_SIGNALS: dict[str, str] = {
    r"nave(?:s)? industrial(?:es)?": "Naves industriales (techo propio para paneles)",
    r"planta(?:s)? (?:de )?(?:produccion|producción|procesamiento)": "Planta de produccion propia",
    r"\b24/7\b|24 horas|las 24 horas|tres turnos|turnos? (?:rotativos|continuos)": "Operacion continua (consumo electrico alto)",
    r"c[aá]maras? (?:de )?(?:refrigeraci[oó]n|congelaci[oó]n|frigor[ií]fica)": "Camaras de refrigeracion (consumo intensivo)",
    r"cuartos? fr[ií]os?": "Cuartos frios (consumo intensivo)",
    r"\bCEDIS\b|centro(?:s)? de distribuci[oó]n": "Centro de distribucion / CEDIS",
    r"\d[\d,\.]*\s*(?:m2|m²|metros cuadrados)": "Superficie de instalaciones declarada",
    r"hect[aá]reas?": "Terreno propio (hectareas)",
    r"inyecci[oó]n de pl[aá]stico|moldeo": "Inyeccion de plastico (maquinaria de alto consumo)",
    r"empacadora|empaque agr[ií]cola": "Empacadora (refrigeracion + maquinaria)",
    r"manufactura|f[aá]brica|fabricamos": "Manufactura propia",
    r"bodega(?:s)? propia(?:s)?|almac[eé]n(?:es)? propios?": "Bodegas/almacenes propios",
    r"flotilla|log[ií]stica propia": "Operacion logistica propia",
    r"GDMTH|GDMTO|PDBT|media tensi[oó]n": "Mencion de tarifa industrial CFE (lead caliente)",
}

# RED FLAGS: descartar sin contactar (PARTE 2 del brief).
RED_FLAG_SIGNALS: dict[str, str] = {
    r"coworking|co-working|wework|regus|oficina(?:s)? compartida(?:s)?": "Opera en coworking (sin techo propio)",
    r"plaza comercial|centro comercial|\blocal \d+\b|\bmall\b": "Ubicada en plaza comercial (sin techo propio)",
    r"torre [a-z0-9]|piso \d{1,2}\b|oficina \d{3,}": "Oficina en edificio corporativo vertical",
    r"oficina(?:s)? virtual(?:es)?|domicilio fiscal compartido": "Oficina virtual (sin instalaciones)",
    r"renta de oficinas|oficinas en renta": "Contexto de oficinas rentadas",
}

_ABOUT_LINK_HINTS = (
    "nosotros", "acerca", "about", "quienes-somos", "quienes_somos",
    "empresa", "historia", "instalaciones", "infraestructura", "planta",
)


def _scan_signals(text: str, signals: dict[str, str]) -> list[dict]:
    """Busca cada patron y devuelve hallazgos con extracto de contexto."""
    findings: list[dict] = []
    for pattern, meaning in signals.items():
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            start = max(0, match.start() - EXCERPT_RADIUS)
            end = min(len(text), match.end() + EXCERPT_RADIUS)
            excerpt = " ".join(text[start:end].split())
            findings.append({"signal": meaning, "excerpt": excerpt})
            break  # un extracto por senal es suficiente para el LLM
    return findings


async def _fetch_visible_text(client: httpx.AsyncClient, url: str) -> str | None:
    html = await fetch_html(client, url)  # tolera certificados SSL vencidos
    if html is None:
        return None
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)[:MAX_PAGE_TEXT]


def _find_about_links(html_text_soup_source: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html_text_soup_source, "lxml")
    base_host = urlparse(base_url).netloc
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        target = urljoin(base_url, a["href"])
        if urlparse(target).netloc != base_host:
            continue
        haystack = f"{a['href'].lower()} {a.get_text(' ', strip=True).lower()}"
        if any(hint in haystack for hint in _ABOUT_LINK_HINTS) and target not in found:
            found.append(target)
    return found[:MAX_ABOUT_PAGES]


async def deep_research_company(
    company_name: str,
    website: str | None = None,
    city: str | None = None,
) -> str:
    """Tool principal del Agente 2: investiga una empresa en la web real.

    Args:
        company_name: razon social o nombre comercial.
        website: web oficial si ya se conoce (la aporta el Agente 1).
        city: ciudad para desambiguar empresas homonimas.

    Returns:
        Reporte de texto consolidado (fuentes + evidencia positiva +
        red flags + extractos) listo para que el LLM del Agente 2 emita
        el veredicto de calificacion.
    """
    sections: list[str] = [
        f"=== REPORTE DE INVESTIGACION: {company_name} ===",
        f"Web conocida: {website or 'ninguna'} | Ciudad: {city or 'desconocida'}",
    ]
    positive_findings: list[dict] = []
    red_flag_findings: list[dict] = []

    # ------------------------------------------------------------------
    # 1) Busquedas web reales sobre la empresa
    # ------------------------------------------------------------------
    geo = f" {city}" if city else " México"
    search_queries = [
        f'"{company_name}"{geo} planta OR "nave industrial" OR instalaciones',
        f'"{company_name}"{geo} oficinas OR coworking OR "plaza comercial"',
    ]

    search_lines: list[str] = []
    for i, query in enumerate(search_queries):
        results = await web_search(query, max_results=5)
        for r in results:
            line = f"- [{r['engine']}] {r['title']} | {r['url']}"
            if r.get("snippet"):
                line += f"\n  Snippet: {r['snippet'][:300]}"
            search_lines.append(line)
            combined = f"{r['title']} {r.get('snippet', '')}"
            positive_findings.extend(_scan_signals(combined, POSITIVE_SIGNALS))
            red_flag_findings.extend(_scan_signals(combined, RED_FLAG_SIGNALS))
        if i < len(search_queries) - 1:
            await jitter_sleep(3.0, 7.0)

    sections.append(
        "--- RESULTADOS DE BUSQUEDA WEB ---\n"
        + ("\n".join(search_lines) if search_lines else "(sin resultados de busqueda)")
    )

    # ------------------------------------------------------------------
    # 2) La propia web de la empresa (portada + "acerca de")
    # ------------------------------------------------------------------
    if website and website.startswith(("http://", "https://")):
        async with httpx.AsyncClient(
            headers=build_browser_headers(), timeout=REQUEST_TIMEOUT, follow_redirects=True
        ) as client:
            home_html = await fetch_html(client, website)
            if home_html is None:
                sections.append(
                    "--- WEB DE LA EMPRESA ---\nNo accesible (ver detalle en logs)."
                )

            if home_html:
                page_texts: list[tuple[str, str]] = []
                soup = BeautifulSoup(home_html, "lxml")
                for tag in soup(["script", "style", "noscript", "svg"]):
                    tag.decompose()
                page_texts.append((website, soup.get_text(" ", strip=True)[:MAX_PAGE_TEXT]))

                for about_url in _find_about_links(home_html, website):
                    await jitter_sleep(2.0, 5.0)
                    text = await _fetch_visible_text(client, about_url)
                    if text:
                        page_texts.append((about_url, text))

                web_lines = []
                for page_url, text in page_texts:
                    page_positive = _scan_signals(text, POSITIVE_SIGNALS)
                    page_flags = _scan_signals(text, RED_FLAG_SIGNALS)
                    positive_findings.extend(page_positive)
                    red_flag_findings.extend(page_flags)
                    web_lines.append(
                        f"- Pagina analizada: {page_url} "
                        f"({len(page_positive)} senales positivas, {len(page_flags)} red flags)"
                    )
                    # Primeros ~600 chars de texto: contexto general para el LLM.
                    web_lines.append(f"  Extracto inicial: {text[:600]}")
                sections.append("--- WEB DE LA EMPRESA ---\n" + "\n".join(web_lines))

    # ------------------------------------------------------------------
    # 3) Consolidado de evidencia
    # ------------------------------------------------------------------
    def _dedupe(findings: list[dict]) -> list[dict]:
        seen, unique = set(), []
        for f in findings:
            if f["signal"] not in seen:
                seen.add(f["signal"])
                unique.append(f)
        return unique

    positive_findings = _dedupe(positive_findings)
    red_flag_findings = _dedupe(red_flag_findings)

    sections.append(
        "--- EVIDENCIA POSITIVA (perfil de cliente Jofra) ---\n"
        + ("\n".join(f"+ {f['signal']}\n  Evidencia: \"...{f['excerpt']}...\""
                     for f in positive_findings) or "(ninguna encontrada)")
    )
    sections.append(
        "--- RED FLAGS (motivos de descarte) ---\n"
        + ("\n".join(f"! {f['signal']}\n  Evidencia: \"...{f['excerpt']}...\""
                     for f in red_flag_findings) or "(ninguna encontrada)")
    )
    sections.append(
        "--- INSTRUCCION PARA EL EVALUADOR ---\n"
        "Con la evidencia anterior decide si la empresa encaja en el perfil de "
        "cliente de paneles solares industriales: instalaciones/techo propio y "
        "alto consumo electrico => CALIFICA. Coworking, plaza comercial, edificio "
        "vertical o empresa muy pequena => DESCARTA. Si la evidencia es "
        "insuficiente, indica que falta investigar."
    )

    report = "\n\n".join(sections)
    logger.info(
        "Investigacion de %r: %s senales positivas, %s red flags, %s chars de reporte.",
        company_name, len(positive_findings), len(red_flag_findings), len(report),
    )
    return report
