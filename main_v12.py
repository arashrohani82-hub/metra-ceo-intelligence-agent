"""V12 runtime patch for Metra CEO dashboard.

Keeps the stable V11 dashboard, fixes USD/CAD using the correct Bank of Canada
series (FXUSDCAD), and cross-checks it against the inverse CAD/USD series when
available. Flights remain isolated and only run on explicit user request / long TTL.
"""

import main as app

app.VERSION = "CEO-BOT-V12-FX-FIX"


def fetch_usdcad_boc_v12():
    """Return USD/CAD from Bank of Canada using the correct series.

    FXUSDCAD = Canadian dollars per 1 US dollar (the value the dashboard needs).
    FXCADUSD = US dollars per 1 Canadian dollar and is used only as a cross-check.
    """
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

    # Independent mathematical cross-check against CAD/USD from the same official
    # source. A mismatch beyond 0.5% is rejected instead of being displayed.
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


# Replace only the broken FX function. All V11 market/Telegram/rendering logic stays intact.
app.fetch_usdcad_boc = fetch_usdcad_boc_v12


if __name__ == "__main__":
    app.startup()
