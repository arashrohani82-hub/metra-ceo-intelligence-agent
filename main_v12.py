"""V12 runtime patch for Metra CEO dashboard.

Keeps the stable market engine, fixes USD/CAD, enlarges mobile dashboard figures,
and appends official Montreal permit-market analytics.
"""

import math
import main as app
import permits_patch

app.VERSION = "CEO-BOT-V12-FX-FIX"


def fetch_usdcad_boc_v12():
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
                    raise RuntimeError(f"Bank of Canada FX cross-check mismatch: {current} vs {implied}")
    except RuntimeError:
        raise
    except Exception as exc:
        print(f"[{app.VERSION}] FX inverse cross-check skipped: {type(exc).__name__}: {exc}", flush=True)
    return current, change, url


app.fetch_usdcad_boc = fetch_usdcad_boc_v12
_base_render_dashboard = app.render_dashboard


def truncate_3_decimals(value):
    return math.trunc(float(value) * 1000) / 1000.0


def render_dashboard_v12(s):
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

        # Mobile-first card typography: make the actual figures noticeably larger.
        app.rounded(d, box, accent)
        x1, y1, x2, y2 = box
        d.text((x1 + 18, y1 + 14), app.rtl(title), font=app.font(27, True), fill=(236, 241, 245))
        d.text((x1 + 18, y1 + 59), value, font=app.font(44, True), fill=(245, 248, 250))
        if sub:
            d.text((x1 + 18, y1 + 113), app.rtl(sub), font=app.font(21), fill=(175, 188, 200))
        if change is not None:
            d.text((x1 + 18, y2 - 40), app.pct(change), font=app.font(22, True), fill=app.cchange(change))
        if b:
            d.text((x2 - 50, y1 + 16), b, font=app.font(19, True), fill=(190, 200, 210))

    app.metric = metric_compact
    try:
        return _base_render_dashboard(view)
    finally:
        app.metric = original_metric


app.render_dashboard = render_dashboard_v12

_base_show_dashboard = app.show_dashboard


def show_dashboard_with_permits(chat_id):
    current_render = app.render_dashboard

    def final_render(snapshot):
        raw = current_render(snapshot)
        return permits_patch.append_permits(app, raw)

    app.render_dashboard = final_render
    try:
        return _base_show_dashboard(chat_id)
    finally:
        app.render_dashboard = current_render


app.show_dashboard = show_dashboard_with_permits
app.fetch_montreal_permits = lambda force=False: permits_patch.fetch_permits(app, force=force)


if __name__ == "__main__":
    app.startup()
