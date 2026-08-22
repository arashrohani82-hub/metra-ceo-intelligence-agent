"""V12 runtime patch for Metra CEO dashboard.

Keeps the stable V11 dashboard, fixes USD/CAD using the correct Bank of Canada
series (FXUSDCAD), cross-checks it against CAD/USD, and renders Iranian gold
and coin values compactly in million toman without changing stored source data.
"""

import main as app

app.VERSION = "CEO-BOT-V12-FX-FIX"


def fetch_usdcad_boc_v12():
    """Return USD/CAD from Bank of Canada using the correct series."""
    url = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
    data = app.safe_get(url, params={"recent": 2}).json()
    obs = data.get("observations") or []
    vals = []
    for row in obs:
        v = app.fv((row.get("FXUSDCAD") or {}).get("v"))
        if v:
            vals.append(v)
    if not vals:
        raise RuntimeError("Bank of Canada FXUSDCAD unavailable")

    current = vals[-1]
    change = app.pct_change(current, vals[-2]) if len(vals) >= 2 else None

    try:
        inv_url = "https://www.bankofcanada.ca/valet/observations/FXCADUSD/json"
        inv_data = app.safe_get(inv_url, params={"recent": 1}).json()
        inv_obs = inv_data.get("observations") or []
        if inv_obs:
            cadusd = app.fv((inv_obs[-1].get("FXCADUSD") or {}).get("v"))
            if cadusd:
                implied = 1.0 / cadusd
                if abs(current - implied) / current > 0.005:
                    raise RuntimeError(
                        f"Bank of Canada FX cross-check mismatch: {current} vs {implied}"
                    )
    except RuntimeError:
        raise
    except Exception as exc:
        print(f"[{app.VERSION}] FX inverse cross-check skipped: {type(exc).__name__}: {exc}", flush=True)

    return current, change, url


app.fetch_usdcad_boc = fetch_usdcad_boc_v12

# Display-only conversion. Stored/source values remain untouched in toman.
_base_render_dashboard = app.render_dashboard


def render_dashboard_v12(s):
    view = dict(s)
    coin = app.fv(view.get("emami_coin_toman"))
    gold18 = app.fv(view.get("iran_gold18_toman_g"))
    if coin is not None:
        view["emami_coin_toman"] = coin / 1_000_000.0
    if gold18 is not None:
        view["iran_gold18_toman_g"] = gold18 / 1_000_000.0

    original_metric = app.metric

    def metric_compact(d, box, title, value, sub="", change=None, accent=(46, 204, 113), b=""):
        if title == "سکه امامی":
            sub = "میلیون تومان"
            try:
                value = f"{float(value.replace(',', '')):.3f}"
            except Exception:
                pass
        elif title == "طلای 18 عیار":
            sub = "میلیون تومان / گرم"
            try:
                value = f"{float(value.replace(',', '')):.3f}"
            except Exception:
                pass
        return original_metric(d, box, title, value, sub, change, accent, b)

    app.metric = metric_compact
    try:
        return _base_render_dashboard(view)
    finally:
        app.metric = original_metric


app.render_dashboard = render_dashboard_v12


if __name__ == "__main__":
    app.startup()
