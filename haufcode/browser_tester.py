"""
HaufCode — browser_tester.py  (v0.5, NEW — scaffolding V4)

Tests fonctionnels frontend via Playwright headless.

Pourquoi ? Pendant webbattlemap, le Tester se contentait de `curl localhost:8085`
(retour 200 = PASS) sans jamais vérifier que les boutons étaient là, que le
canvas répondait au clic, etc. Pour un projet front-end, cela passait à côté
de l'essentiel.

Ce module est OPTIONNEL : il s'active si Playwright est installé. Sinon, il
expose des stubs qui retournent BLOCKED avec un message explicatif.

Pour activer :
  pip install playwright
  playwright install chromium

Utilisation typique (depuis le Tester ou un script séparé) :
    from haufcode.browser_tester import smoke_test
    result = smoke_test(
        url="http://localhost:8085",
        expected_selectors=["#import-button", "canvas"],
        screenshot_path="/tmp/proof.png",
    )
    if result.ok:
        print("Frontend OK")
    else:
        print(f"Échecs : {result.failures}")

Le résultat (BrowserTestResult) peut être directement injecté dans le rapport
du Tester comme preuve fonctionnelle.
"""
from dataclasses import dataclass, field
from pathlib import Path

# Détection paresseuse de Playwright
try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


@dataclass
class BrowserTestResult:
    url: str
    ok: bool = False
    status_code: int = 0
    failures: list = field(default_factory=list)
    successes: list = field(default_factory=list)
    screenshot_path: str = ""
    console_errors: list = field(default_factory=list)
    network_errors: list = field(default_factory=list)
    final_html_excerpt: str = ""
    duration_ms: int = 0

    def to_report(self) -> str:
        lines = [f"🌐 BROWSER_TEST: {self.url} — "
                 f"{'✅ OK' if self.ok else '❌ FAIL'} ({self.duration_ms}ms)"]
        if self.status_code:
            lines.append(f"  HTTP status: {self.status_code}")
        for s in self.successes:
            lines.append(f"  ✅ {s}")
        for f in self.failures:
            lines.append(f"  ❌ {f}")
        for ce in self.console_errors[:5]:
            lines.append(f"  ⚠️  console: {ce}")
        for ne in self.network_errors[:5]:
            lines.append(f"  ⚠️  network: {ne}")
        if self.screenshot_path:
            lines.append(f"  📸 screenshot: {self.screenshot_path}")
        return "\n".join(lines)


def smoke_test(
    url: str,
    expected_selectors: list = None,
    screenshot_path: str = "",
    wait_ms: int = 1500,
    timeout_ms: int = 10_000,
) -> BrowserTestResult:
    """
    Test fonctionnel basique d'une page web :
      - charge l'URL ;
      - attend `wait_ms` ms pour laisser le JS s'exécuter ;
      - vérifie chaque sélecteur CSS de `expected_selectors` ;
      - capture les erreurs console/network ;
      - prend un screenshot ;
      - extrait un résumé HTML.

    Renvoie un BrowserTestResult.
    """
    expected_selectors = expected_selectors or []
    result = BrowserTestResult(url=url)

    if not PLAYWRIGHT_AVAILABLE:
        result.failures.append(
            "Playwright non installé. Pour activer les tests browser : "
            "`pip install playwright && playwright install chromium`."
        )
        return result

    import time
    from playwright.sync_api import sync_playwright

    t0 = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            console_errors = []
            page.on("console",
                    lambda msg: console_errors.append(msg.text)
                    if msg.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            network_errors = []
            page.on("response",
                    lambda resp: (network_errors.append(
                        f"{resp.status} {resp.url}")
                        if resp.status >= 400 else None))

            response = page.goto(url, timeout=timeout_ms,
                                 wait_until="domcontentloaded")
            if response:
                result.status_code = response.status

            page.wait_for_timeout(wait_ms)

            # Vérification des sélecteurs
            for sel in expected_selectors:
                try:
                    page.wait_for_selector(sel, timeout=2000, state="attached")
                    result.successes.append(f"Sélecteur '{sel}' présent")
                except Exception as exc:
                    result.failures.append(f"Sélecteur '{sel}' absent : {exc}")

            # Screenshot
            if screenshot_path:
                Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=screenshot_path, full_page=True)
                result.screenshot_path = screenshot_path

            # HTML extract
            html = page.content()
            result.final_html_excerpt = html[:2000]

            result.console_errors = console_errors
            result.network_errors = network_errors
            result.duration_ms = int((time.time() - t0) * 1000)

            browser.close()

        result.ok = (
            (200 <= result.status_code < 400)
            and not result.failures
            and not result.console_errors
        )

    except Exception as exc:
        result.failures.append(f"Exception Playwright : {exc}")

    return result


def click_and_check(
    url: str,
    click_selector: str,
    expected_after_click: list = None,
    screenshot_path: str = "",
    timeout_ms: int = 10_000,
) -> BrowserTestResult:
    """
    Charge l'URL, clique sur `click_selector`, vérifie que les éléments
    `expected_after_click` apparaissent (sélecteurs CSS).
    """
    result = BrowserTestResult(url=url)

    if not PLAYWRIGHT_AVAILABLE:
        result.failures.append(
            "Playwright non installé (cf. `pip install playwright`)."
        )
        return result

    import time
    from playwright.sync_api import sync_playwright

    expected_after_click = expected_after_click or []
    t0 = time.time()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(url, timeout=timeout_ms)
            try:
                page.click(click_selector, timeout=3000)
                result.successes.append(f"Clic sur '{click_selector}' OK")
            except Exception as exc:
                result.failures.append(f"Clic sur '{click_selector}' impossible : {exc}")
                browser.close()
                result.duration_ms = int((time.time() - t0) * 1000)
                return result

            page.wait_for_timeout(500)

            for sel in expected_after_click:
                try:
                    page.wait_for_selector(sel, timeout=2000)
                    result.successes.append(f"Après clic, '{sel}' visible")
                except Exception as exc:
                    result.failures.append(f"Après clic, '{sel}' absent : {exc}")

            if screenshot_path:
                Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=screenshot_path)
                result.screenshot_path = screenshot_path

            browser.close()

        result.ok = bool(result.successes) and not result.failures
        result.duration_ms = int((time.time() - t0) * 1000)

    except Exception as exc:
        result.failures.append(f"Exception : {exc}")

    return result


def is_available() -> bool:
    """Permet aux callers de skipper proprement les tests browser."""
    return PLAYWRIGHT_AVAILABLE


def install_hint() -> str:
    return (
        "Pour activer les tests browser :\n"
        "  pip install playwright\n"
        "  playwright install chromium\n"
        "Puis ajoute dans le prompt Tester : « si l'app est web, lance "
        "browser_tester.smoke_test(...) avant de PASS »."
    )
