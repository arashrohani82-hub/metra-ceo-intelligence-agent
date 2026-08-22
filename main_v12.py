"""V12 runtime patch for Metra CEO dashboard.

Keeps the stable V11 dashboard, fixes USD/CAD using the correct Bank of Canada
series (FXUSDCAD), cross-checks it against CAD/USD, and renders Iranian gold
and coin values compactly in million toman without changing stored source data.
"""

import math
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

_base_render_dashboard = app.render_dashboard


def truncate_3_decimals(value):
    """Keep exactly 3 decimals by truncation, never rounding."""
    return math.trunc(float(value) * 1000) / 1000.0


def render_dashboard_v12(s):
    # Keep the original raw toman values intact. The base renderer formats numbers
    # to zero decimals before calling metric(), so for these two cards we bypass
    # that already-rounded display string and rebuild it from the raw snapshot.
    view = dict(s)
    raw_coin = app.fv(s.get("emami_coin_toman"))
    raw_gold18 = app.fv(s.get("iran_gold18_toman_g"))

    original_metric = app.metric

    def metric_compact(d, box, title, value, sub="", change=None, accent=(46, 204, 113), b=""):
        if title == "سکه امامی":
            sub = "میلیون تومان"
            if raw_coin is not None:
                value = f"{truncate_3_decimals(raw_coin / 1_000_000.0):.3f}"
        elif title == "طلای 18 عیار":
            sub = "میلیون تومان / گرم"
            if raw_gold18 is not None:
                value = f"{truncate_3_decimals(raw_gold18 / 1_000_000.0):.3f}"
        return original_metric(d, box, title, value, sub, change, accent, b)

    app.metric = metric_compact
    try:
        return _base_render_dashboard(view)
    finally:
        app.metric = original_metric


app.render_dashboard = render_dashboard_v12


if __name__ == "__main__":
    app.startup()
