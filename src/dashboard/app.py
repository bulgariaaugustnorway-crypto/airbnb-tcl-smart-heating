"""Mobile-friendly single-page HTML/CSS/JS frontend for the Owner Dashboard."""

import html
import json
from typing import Dict, Any, Optional



def _script_json(value):
    """Serialize JSON without allowing an HTML parser to terminate its script."""
    return (json.dumps(value).replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_login_html(error_message: Optional[str] = None, rate_limit_remaining: int = 0) -> str:
    """Render mobile-friendly login screen with security alerts."""
    error_html = ""
    if rate_limit_remaining > 0:
        mins = rate_limit_remaining // 60
        secs = rate_limit_remaining % 60
        error_html = f"""
        <div class="alert alert-danger" role="alert">
            <strong>Account Locked:</strong> Too many failed attempts. Please wait {mins}m {secs}s before retrying.
        </div>
        """
    elif error_message:
        error_html = f"""
        <div class="alert alert-danger" role="alert">
            {html.escape(error_message)}
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>TCL Airbnb Climate Dashboard - Login</title>
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --danger: #ef4444;
            --border: #334155;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg-color);
            color: var(--text-primary);
            font-family: var(--font-family);
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 16px;
        }}
        .login-card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 32px 24px;
            width: 100%;
            max-width: 400px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
        }}
        .header {{ text-align: center; margin-bottom: 24px; }}
        .header h1 {{ font-size: 20px; font-weight: 700; color: #fff; margin-bottom: 6px; }}
        .header p {{ font-size: 13px; color: var(--text-secondary); }}
        .form-group {{ margin-bottom: 20px; }}
        label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--text-secondary); }}
        input[type="password"] {{
            width: 100%;
            padding: 14px 16px;
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 10px;
            color: #fff;
            font-size: 16px;
            outline: none;
            transition: border-color 0.2s;
        }}
        input[type="password"]:focus {{ border-color: var(--primary); }}
        button.btn-login {{
            width: 100%;
            padding: 14px;
            background: var(--primary);
            border: none;
            border-radius: 10px;
            color: #fff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }}
        button.btn-login:hover {{ background: var(--primary-hover); }}
        .alert {{
            padding: 12px 14px;
            border-radius: 8px;
            font-size: 13px;
            margin-bottom: 18px;
            line-height: 1.4;
        }}
        .alert-danger {{ background: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); color: #fca5a5; }}
        .footer {{ text-align: center; margin-top: 24px; font-size: 11px; color: var(--text-secondary); }}
    </style>
</head>
<body>
    <div class="login-card">
        <div class="header">
            <h1>Sofia Apartment HVAC</h1>
            <p>Owner Control Dashboard</p>
        </div>
        {error_html}
        <form method="POST" action="/api/login">
            <div class="form-group">
                <label for="password">Owner Access Password</label>
                <input type="password" id="password" name="password" required autocomplete="current-password" autofocus placeholder="Enter password...">
            </div>
            <button type="submit" class="btn-login">Sign In</button>
        </form>
        <div class="footer">
            AWS Serverless &bull; Europe/Sofia Time &bull; AES-256 Protected
        </div>
    </div>
</body>
</html>
"""


def render_dashboard_html(
    csrf_token: str,
    status_data: Dict[str, Any],
    config_data: Dict[str, Any]
) -> str:
    """Render full mobile-first responsive owner management dashboard."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>TCL Climate Control - Owner Dashboard</title>
    <style>
        :root {{
            --bg-color: #0b1329;
            --surface: #1e293b;
            --surface-hover: #293548;
            --border: #334155;
            --primary: #3b82f6;
            --primary-dark: #2563eb;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: var(--font-family);
            padding: 12px;
            padding-bottom: 90px;
            line-height: 1.5;
        }}
        .container {{ max-width: 800px; margin: 0 auto; }}

        /* Top Bar */
        .top-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            margin-bottom: 14px;
        }}
        .top-bar-title h1 {{ font-size: 17px; font-weight: 700; }}
        .top-bar-title p {{ font-size: 12px; color: var(--text-muted); }}
        .btn-logout {{
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 12px;
            cursor: pointer;
            min-height: 38px;
        }}

        /* Card Component */
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 18px 16px;
            margin-bottom: 14px;
        }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
        }}
        .card-title {{ font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 8px; }}
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
        }}
        .badge-success {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #059669; }}
        .badge-warning {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #d97706; }}
        .badge-danger {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #dc2626; }}
        .badge-info {{ background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid #2563eb; }}
        .badge-dimmed {{ background: rgba(100, 116, 139, 0.2); color: #94a3b8; border: 1px solid #475569; }}

        /* Live Device Grid */
        .device-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
        @media (min-width: 600px) {{
            .device-grid {{ grid-template-columns: 1fr 1fr; }}
        }}
        .device-box {{
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 14px;
        }}
        .device-box-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
        .device-name {{ font-size: 14px; font-weight: 600; }}
        .temp-display {{
            display: flex;
            align-items: baseline;
            gap: 12px;
            margin: 10px 0;
        }}
        .temp-target {{ font-size: 28px; font-weight: 800; color: #fff; }}
        .temp-ambient {{ font-size: 14px; color: var(--text-muted); }}
        .device-meta {{ font-size: 12px; color: var(--text-muted); display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 10px; border-top: 1px solid #1e293b; padding-top: 8px; }}

        /* Form Controls */
        .form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px; }}
        @media (max-width: 500px) {{
            .form-row {{ grid-template-columns: 1fr; }}
        }}
        .form-field {{ margin-bottom: 12px; }}
        .form-field label {{ display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 600; }}
        .form-control {{
            width: 100%;
            padding: 12px;
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 8px;
            color: #fff;
            font-size: 15px;
            outline: none;
            min-height: 48px; /* Mobile touch target standard */
        }}
        .form-control:focus {{ border-color: var(--primary); }}

        /* Switches and Toggles */
        .toggle-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            min-height: 48px;
        }}
        .toggle-row:last-child {{ border-bottom: none; }}
        .toggle-label {{ font-size: 13px; font-weight: 500; }}
        .toggle-sub {{ font-size: 11px; color: var(--text-muted); }}
        .switch {{
            position: relative;
            display: inline-block;
            width: 52px;
            height: 30px;
        }}
        .switch input {{ opacity: 0; width: 0; height: 0; }}
        .slider {{
            position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
            background-color: #334155; transition: .2s; border-radius: 30px;
        }}
        .slider:before {{
            position: absolute; content: ""; height: 22px; width: 22px; left: 4px; bottom: 4px;
            background-color: white; transition: .2s; border-radius: 50%;
        }}
        input:checked + .slider {{ background-color: var(--primary); }}
        input:checked + .slider:before {{ transform: translateX(22px); }}

        /* Quick Action Buttons */
        .btn-group {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
        .btn-quick {{
            flex: 1;
            min-width: 60px;
            min-height: 44px;
            padding: 8px 12px;
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 8px;
            color: #fff;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            text-align: center;
        }}
        .btn-quick.active {{ background: var(--primary); border-color: var(--primary); }}

        /* Sticky Footer Bar */
        .sticky-footer {{
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: #1e293bee;
            backdrop-filter: blur(8px);
            border-top: 1px solid var(--border);
            padding: 12px 16px;
            display: flex;
            justify-content: center;
            z-index: 100;
        }}
        .btn-save {{
            width: 100%;
            max-width: 600px;
            padding: 14px;
            background: var(--primary);
            border: none;
            border-radius: 10px;
            color: #fff;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.4);
            min-height: 48px;
        }}
        .btn-save:hover {{ background: var(--primary-dark); }}

        /* Toast Notifications */
        #toast {{
            visibility: hidden;
            min-width: 250px;
            background-color: #10b981;
            color: #fff;
            text-align: center;
            border-radius: 8px;
            padding: 12px 16px;
            position: fixed;
            z-index: 200;
            left: 50%;
            top: 20px;
            transform: translateX(-50%);
            font-size: 14px;
            font-weight: 600;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }}
        #toast.show {{ visibility: visible; animation: fadein 0.3s, fadeout 0.5s 2.5s; }}
        @keyframes fadein {{ from {{ top: 0; opacity: 0; }} to {{ top: 20px; opacity: 1; }} }}
        @keyframes fadeout {{ from {{ top: 20px; opacity: 1; }} to {{ top: 0; opacity: 0; }} }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Top Bar -->
        <div class="top-bar">
            <div class="top-bar-title">
                <h1>Sofia Apartment</h1>
                <p id="clock-display">Europe/Sofia &bull; Automated Climate</p>
                <div style="font-size: 11px; color: var(--text-muted); margin-top: 3px;">
                    Last Telemetry: <strong id="telemetry-timestamp-text" style="color: #cbd5e1;">Syncing...</strong>
                </div>
            </div>
            <form method="POST" action="/api/logout">
                <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
                <button type="submit" class="btn-logout">Logout</button>
            </form>
        </div>

        <div id="toast">Settings Saved Successfully!</div>

        <!-- Stale Telemetry Warning Alert -->
        <div id="stale-telemetry-alert" style="display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); border-radius: 10px; padding: 10px 14px; margin-bottom: 14px; font-size: 13px; color: #fca5a5;">
            ⚠️ <strong>Telemetry Stale (>20m old):</strong> Last data was recorded <span id="stale-telemetry-age">--</span>. Showing cached state from previous scheduler cycle.
        </div>

        <!-- Cloud Safety Banner -->
        <div id="safety-banner" style="display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-radius: 12px; margin-bottom: 14px; font-size: 13px; font-weight: 600; border: 1px solid #d97706; background: rgba(245, 158, 11, 0.12);">
            <div style="display: flex; align-items: center; gap: 8px;">
                <span id="safety-icon" style="font-size: 18px;">🛡️</span>
                <span id="safety-label">Safe Mode: Telemetry monitored safely</span>
            </div>
            <span id="safety-badge" class="badge badge-warning">DRY RUN</span>
        </div>

        <!-- System Status Banner Card -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">System Status</div>
                <span id="badge-system-state" class="badge badge-info">INITIALIZING</span>
            </div>
            <div id="status-reason-text" style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">Loading live decision...</div>

            <!-- Live Devices -->
            <div class="device-grid">
                <!-- Living Room -->
                <div class="device-box" id="device-living-room">
                    <div class="device-box-header">
                        <div>
                            <span class="device-name">Living Room</span>
                            <div style="font-size: 11px; font-family: monospace; color: var(--text-muted); margin-top: 2px;">Hardware ID: <strong id="lr-device-id" style="color: #cbd5e1;">demo_living_room</strong></div>
                        </div>
                        <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap; justify-content: flex-end;">
                            <span class="badge badge-success" id="lr-online-badge">ONLINE</span>
                            <span class="badge badge-success" id="lr-power-badge">POWER ON</span>
                            <span class="badge badge-danger" id="lr-guest-override-badge" style="display: none;">GUEST OFF LOCK</span>
                            <span class="badge badge-info" id="lr-temp-freshness-badge">FRESH</span>
                        </div>
                    </div>
                    <div class="temp-display" style="display: flex; flex-direction: column; gap: 4px; padding: 6px 0;">
                        <div style="display: flex; justify-content: space-between; align-items: baseline;">
                            <span style="font-size: 12px; color: var(--text-muted); font-weight: 500;">Target Setpoint:</span>
                            <span class="temp-target" id="lr-target-temp" style="font-size: 22px; font-weight: 700; color: #fff;">21°C</span>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: baseline;">
                            <span style="font-size: 12px; color: var(--text-muted); font-weight: 500;">Measured Room Temp:</span>
                            <div style="text-align: right;">
                                <span class="temp-ambient" id="lr-ambient-temp" style="font-size: 14px; font-weight: 600; color: #38bdf8;">--°C</span>
                                <div id="lr-temp-age" style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">(measured --)</div>
                            </div>
                        </div>
                    </div>
                    <div class="device-meta">
                        <span>Mode: <strong id="lr-mode">Heat</strong></span>
                        <span>Fan: <strong id="lr-fan">Auto</strong></span>
                        <span>LED: <strong id="lr-led">OFF</strong></span>
                        <span>Beep: <strong id="lr-beep">OFF</strong></span>
                    </div>
                    <div id="lr-override-alert" style="display: none; background: rgba(245, 158, 11, 0.15); border: 1px solid #d97706; border-radius: 6px; padding: 6px 8px; font-size: 11px; color: #fbbf24; margin-top: 8px;">
                        ⚠️ <strong id="lr-override-label">OVERRIDE:</strong> <span id="lr-override-text">Adjusted</span>
                    </div>
                    <div id="lr-warnings-box" style="display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid #dc2626; border-radius: 6px; padding: 6px 8px; font-size: 11px; color: #fca5a5; margin-top: 8px;">
                        <div id="lr-warnings-text"></div>
                    </div>
                </div>

                <!-- Bedroom -->
                <div class="device-box" id="device-bedroom">
                    <div class="device-box-header">
                        <div>
                            <span class="device-name">Bedroom</span>
                            <div style="font-size: 11px; font-family: monospace; color: var(--text-muted); margin-top: 2px;">Hardware ID: <strong id="br-device-id" style="color: #cbd5e1;">demo_bedroom</strong></div>
                        </div>
                        <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap; justify-content: flex-end;">
                            <span class="badge badge-success" id="br-online-badge">ONLINE</span>
                            <span class="badge badge-success" id="br-power-badge">POWER ON</span>
                            <span class="badge badge-danger" id="br-guest-override-badge" style="display: none;">GUEST OFF LOCK</span>
                            <span class="badge badge-info" id="br-temp-freshness-badge">FRESH</span>
                        </div>
                    </div>
                    <div class="temp-display" style="display: flex; flex-direction: column; gap: 4px; padding: 6px 0;">
                        <div style="display: flex; justify-content: space-between; align-items: baseline;">
                            <span style="font-size: 12px; color: var(--text-muted); font-weight: 500;">Target Setpoint:</span>
                            <span class="temp-target" id="br-target-temp" style="font-size: 22px; font-weight: 700; color: #fff;">21°C</span>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: baseline;">
                            <span style="font-size: 12px; color: var(--text-muted); font-weight: 500;">Measured Room Temp:</span>
                            <div style="text-align: right;">
                                <span class="temp-ambient" id="br-ambient-temp" style="font-size: 14px; font-weight: 600; color: #38bdf8;">--°C</span>
                                <div id="br-temp-age" style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">(measured --)</div>
                            </div>
                        </div>
                    </div>
                    <div class="device-meta">
                        <span>Mode: <strong id="br-mode">Heat</strong></span>
                        <span>Fan: <strong id="br-fan">Auto</strong></span>
                        <span>LED: <strong id="br-led">OFF</strong></span>
                        <span>Beep: <strong id="br-beep">OFF</strong></span>
                    </div>
                    <div id="br-override-alert" style="display: none; background: rgba(245, 158, 11, 0.15); border: 1px solid #d97706; border-radius: 6px; padding: 6px 8px; font-size: 11px; color: #fbbf24; margin-top: 8px;">
                        ⚠️ <strong id="br-override-label">OVERRIDE:</strong> <span id="br-override-text">Adjusted</span>
                    </div>
                    <div id="br-warnings-box" style="display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid #dc2626; border-radius: 6px; padding: 6px 8px; font-size: 11px; color: #fca5a5; margin-top: 8px;">
                        <div id="br-warnings-text"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Adaptive Comfort Telemetry Live Card -->
        <div class="card" id="adaptive-telemetry-card" style="border-left: 4px solid #10b981;">
            <div class="card-header">
                <div class="card-title">✨ Adaptive Comfort & Preference Memory</div>
                <span id="live-adaptive-status-badge" class="badge badge-success">ACTIVE</span>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 12px; color: var(--text-muted); background: #0f172a; border-radius: 8px; padding: 10px; border: 1px solid var(--border); margin-top: 8px;">
                <div>Living Room Preferred Day: <strong id="live-lr-pref-temp" style="color: #60a5fa;">--</strong></div>
                <div>Bedroom Preferred Day: <strong id="live-br-pref-temp" style="color: #a78bfa;">--</strong></div>
                <div>Bedroom Power-OFF Lock: <strong id="live-br-power-lock" style="color: #34d399;">None</strong></div>
                <div>Last Window Transitions: <strong id="live-last-transitions" style="color: #cbd5e1;">--</strong></div>
            </div>
        </div>

        <!-- Owner Alerts & Notifications Card -->
        <div class="card" id="notifications-card" style="border-left: 4px solid #8b5cf6;">
            <div class="card-header">
                <div class="card-title">🔔 Owner Alerts & Telegram Status</div>
                <span id="badge-notif-status" class="badge badge-dimmed">NOT CONFIGURED</span>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 12px; color: var(--text-muted); background: #0f172a; border-radius: 8px; padding: 10px; border: 1px solid var(--border); margin-top: 8px;">
                <div>Channel: <strong id="notif-channel-text" style="color: #c084fc;">None</strong></div>
                <div>Configured: <strong id="notif-configured-text" style="color: #cbd5e1;">NO</strong></div>
                <div style="grid-column: span 2;">Last Delivery: <strong id="notif-last-delivery-text" style="color: #38bdf8;">Never</strong></div>
            </div>
            <div id="notif-error-box" style="display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); border-radius: 6px; padding: 8px 10px; font-size: 11px; color: #fca5a5; margin-top: 8px;">
                ⚠️ <strong>Delivery Error:</strong> <span id="notif-error-text">--</span>
            </div>
        </div>

        <!-- Airbnb Calendar & Occupancy Status Card -->
        <div class="card" id="occupancy-card" style="border-left: 4px solid var(--primary);">
            <div class="card-header">
                <div class="card-title">📅 Airbnb Calendar & Occupancy</div>
                <div style="display: flex; gap: 6px;">
                    <span id="badge-active-profile" class="badge badge-info" title="Current Property Heating Profile">SKI_DAY</span>
                    <span id="badge-occupancy-status" class="badge badge-info">CHECKING</span>
                </div>
            </div>
            <div id="occupancy-desc-text" style="font-size: 13px; color: var(--text-main); margin-bottom: 12px; font-weight: 500;">
                Checking calendar synchronization...
            </div>

            <!-- Active/Upcoming Stay Profile Assignment -->
            <div id="reservation-profile-box" style="display: none; background: #0f172a; border-radius: 8px; padding: 10px; border: 1px solid var(--border); margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <div style="font-size: 12px;">
                        <span id="res-profile-title" style="font-weight: 600; color: #fff;">Stay Comfort Profile:</span>
                        <span id="res-profile-current" class="badge badge-info" style="margin-left: 6px;">SKI_DAY</span>
                    </div>
                    <div style="display: flex; gap: 6px; align-items: center;">
                        <select id="res-profile-select" class="form-control" style="min-height: 32px; padding: 4px 8px; font-size: 12px; width: auto;">
                            <option value="SKI_DAY">SKI_DAY (21°C Day)</option>
                            <option value="AT_HOME">AT_HOME (23°C Day)</option>
                            <option value="CUSTOM">CUSTOM</option>
                        </select>
                        <button type="button" class="btn-quick" style="padding: 4px 10px; font-size: 12px; min-height: 32px;" onclick="saveStayProfile()">Apply to Stay</button>
                    </div>
                </div>
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 12px; color: var(--text-muted); background: #0f172a; border-radius: 8px; padding: 10px; border: 1px solid var(--border);">
                <div>Confirmed Guest Stays: <strong id="occ-confirmed-count" style="color: #34d399;">0</strong></div>
                <div>Owner/Blocked Dates: <strong id="occ-blocked-count" style="color: #fbbf24;">0</strong></div>
                <div>Next Check-in: <strong id="occ-upcoming-date" style="color: #fff;">None</strong></div>
                <div>iCal Feed Status: <strong id="occ-calendar-freshness" style="color: #34d399;">Active / Fresh</strong></div>
            </div>
        </div>

        <!-- Temporary Owner Presence & Test Mode Card -->
        <div class="card" style="border-left: 4px solid var(--warning);">
            <div class="card-header">
                <div class="card-title">🏡 Owner Presence & Test Override</div>
                <span id="owner-test-status-badge" class="badge badge-info">STANDBY</span>
            </div>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 10px;">
                Activate the occupied heating schedule during personal visits without blocking Airbnb dates. Automatically expires and returns to Airbnb automation.
            </p>

            <!-- Active View -->
            <div id="owner-test-active-view" style="display: none; background: rgba(245,158,11,0.1); border: 1px solid var(--warning); border-radius: 8px; padding: 12px; margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 6px; margin-bottom: 6px;">
                    <div style="font-size: 15px; font-weight: 700; color: #fbbf24;" id="owner-test-heading">Owner Presence Active</div>
                    <span id="owner-test-countdown" class="badge badge-warning">Remaining: --</span>
                </div>
                <div style="font-size: 12px; color: #fff; margin-bottom: 4px;" id="owner-test-expiry-text">Expires: -- Sofia</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 10px;" id="owner-test-details">Following occupied diurnal heating schedule.</div>

                <!-- Expiring Soon Warning Banner -->
                <div id="owner-test-expiring-alert" style="display: none; background: rgba(239, 68, 68, 0.2); border: 1px solid var(--danger); border-radius: 6px; padding: 8px 10px; font-size: 12px; color: #fca5a5; margin-bottom: 10px;">
                    ⚠️ <strong>Expiring Soon:</strong> Owner presence ends in <span id="owner-test-expiring-time">--</span>. System will revert to automated calendar standby unless extended.
                </div>

                <!-- Extend Section -->
                <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap;">
                    <span style="font-size: 12px; color: var(--text-muted); font-weight: 600;">Extend Stay:</span>
                    <button type="button" class="btn-quick" style="padding: 6px 10px; font-size: 12px; min-height: 36px;" onclick="extendOwnerSession(4)">+4h</button>
                    <button type="button" class="btn-quick" style="padding: 6px 10px; font-size: 12px; min-height: 36px;" onclick="extendOwnerSession(12)">+12h</button>
                    <button type="button" class="btn-quick" style="padding: 6px 10px; font-size: 12px; min-height: 36px;" onclick="extendOwnerSession(24)">+24h</button>
                    <button type="button" class="btn-quick" style="padding: 6px 10px; font-size: 12px; min-height: 36px;" onclick="extendOwnerSession(48)">+48h</button>
                </div>

                <button type="button" class="btn-quick" style="background: var(--danger); width: 100%; border: none; min-height: 42px; font-weight: 600;" onclick="cancelOwnerTest()">End Owner Presence (Return to Auto)</button>
            </div>

            <!-- Setup View -->
            <div id="owner-test-setup-view">
                <div class="form-field" style="margin-bottom: 10px;">
                    <label>Override Mode</label>
                    <select class="form-control" id="owner-session-type" onchange="toggleOwnerSessionType()">
                        <option value="OWNER_PRESENT" selected>Owner Presence (Diurnal Schedule: 23°C Day / 20°C Night)</option>
                        <option value="OWNER_TEST">Diagnostic Test Hold (Fixed Setpoint)</option>
                    </select>
                </div>
                <div class="form-row">
                    <div class="form-field">
                        <label>Visit Duration</label>
                        <select class="form-control" id="owner-test-duration">
                            <option value="2">2 Hours (Quick check)</option>
                            <option value="4">4 Hours</option>
                            <option value="8">8 Hours</option>
                            <option value="24" selected>24 Hours (1 Day)</option>
                            <option value="48">48 Hours (Weekend Visit)</option>
                            <option value="72">72 Hours (3 Days Visit)</option>
                        </select>
                    </div>
                    <div class="form-field" id="owner-test-temp-field" style="display: none;">
                        <label>Fixed Hold Temp</label>
                        <select class="form-control" id="owner-test-temp">
                            <option value="20">20°C</option>
                            <option value="21">21°C</option>
                            <option value="22" selected>22°C (Comfort)</option>
                            <option value="23">23°C (Warm)</option>
                            <option value="24">24°C</option>
                        </select>
                    </div>
                </div>
                <button type="button" class="btn-quick" style="background: var(--primary); border: none; width: 100%; font-size: 14px; min-height: 44px;" onclick="startOwnerTest()">Activate Owner Presence</button>
            </div>
        </div>

        <!-- 7-Day HVAC Observation & Energy Monitoring Card -->
        <div class="card" id="monitoring-card" style="border-left: 4px solid #06b6d4;">
            <div class="card-header">
                <div>
                    <div class="card-title">📊 7-Day HVAC Observation & Energy Monitoring</div>
                    <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">
                        Read-only empirical tracking of guest usage, state transitions, and whole-property utility consumption
                    </div>
                </div>
                <span id="badge-monitoring-status" class="badge badge-success">RECORDING</span>
            </div>

            <!-- Hardware Telemetry Disclaimer Banner -->
            <div style="background: rgba(6, 182, 212, 0.1); border: 1px solid #0891b2; border-radius: 8px; padding: 10px 12px; font-size: 12px; color: #a5f3fc; margin-bottom: 12px; line-height: 1.4;">
                ℹ️ <strong>Hardware Power Telemetry Audit:</strong> TCL IoT Cloud exposes <strong>zero electrical power (W) or energy (kWh)</strong> registers for these AC units. AC-specific kWh is marked <strong>UNAVAILABLE</strong> (non-metered). Only whole-apartment electricity meter readings are recorded.
            </div>

            <!-- Quick Metrics Grid -->
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 12px;">
                <div style="background: #0f172a; border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">LR Heating Today</div>
                    <div id="mon-lr-on-today" style="font-size: 18px; font-weight: 700; color: #38bdf8; margin-top: 4px;">0.0h</div>
                    <div style="font-size: 10px; color: #64748b;">approx. ON hours</div>
                </div>
                <div style="background: #0f172a; border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">BR Heating Today</div>
                    <div id="mon-br-on-today" style="font-size: 18px; font-weight: 700; color: #a78bfa; margin-top: 4px;">0.0h</div>
                    <div style="font-size: 10px; color: #64748b;">approx. ON hours</div>
                </div>
                <div style="background: #0f172a; border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Bansko Ambient</div>
                    <div id="mon-weather-today" style="font-size: 18px; font-weight: 700; color: #34d399; margin-top: 4px;">--°C</div>
                    <div style="font-size: 10px; color: #64748b;">outdoor temp</div>
                </div>
                <div style="background: #0f172a; border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-align: center;">
                    <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Device Metering</div>
                    <div style="font-size: 15px; font-weight: 700; color: #94a3b8; margin-top: 6px;">UNAVAILABLE</div>
                    <div style="font-size: 10px; color: #64748b;">TCL cloud limitation</div>
                </div>
            </div>

            <!-- Recent Events Stream -->
            <div style="margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <span style="font-size: 12px; font-weight: 600; color: #cbd5e1;">Recent HVAC Events (State Changes)</span>
                    <span style="font-size: 11px; color: var(--text-muted);">15-min cycle derivation</span>
                </div>
                <div id="mon-recent-events" style="background: #0f172a; border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; font-size: 12px; max-height: 140px; overflow-y: auto;">
                    <div style="color: #64748b; font-style: italic;">No state changes recorded today.</div>
                </div>
            </div>

            <!-- Whole-Property Utility Meter Section -->
            <div style="background: #0f172a; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div>
                        <div style="font-size: 13px; font-weight: 600; color: #fff;">⚡ Whole-Property Electricity Meter</div>
                        <div style="font-size: 11px; color: var(--text-muted);">Includes boiler, kitchen appliances, and HVAC</div>
                    </div>
                    <button type="button" class="btn-quick" style="padding: 4px 10px; font-size: 11px; min-height: 28px;" onclick="toggleMeterEntry()">+ Log Reading</button>
                </div>
                <div id="mon-latest-meter" style="font-size: 12px; color: #cbd5e1; margin-bottom: 6px;">
                    Latest Reading: <strong style="color: #38bdf8;">None recorded</strong>
                </div>

                <!-- Collapsible Meter Entry Form -->
                <div id="mon-meter-entry-form" style="display: none; background: #1e293b; border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-top: 8px;">
                    <div class="form-row">
                        <div class="form-field">
                            <label style="font-size: 11px;">Total Meter (kWh) *</label>
                            <input type="number" step="0.1" class="form-control" id="meter-input-total" placeholder="e.g. 14250.5" style="padding: 6px 10px; font-size: 12px;">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">Recorded By</label>
                            <input type="text" class="form-control" id="meter-input-recorded-by" value="Owner" style="padding: 6px 10px; font-size: 12px;">
                        </div>
                    </div>
                    <div class="form-row" style="margin-top: 6px;">
                        <div class="form-field">
                            <label style="font-size: 11px;">Peak kWh (T1, optional)</label>
                            <input type="number" step="0.1" class="form-control" id="meter-input-peak" placeholder="Optional" style="padding: 6px 10px; font-size: 12px;">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">Off-Peak kWh (T2, optional)</label>
                            <input type="number" step="0.1" class="form-control" id="meter-input-offpeak" placeholder="Optional" style="padding: 6px 10px; font-size: 12px;">
                        </div>
                    </div>
                    <div class="form-field" style="margin-top: 6px;">
                        <label style="font-size: 11px;">Notes</label>
                        <input type="text" class="form-control" id="meter-input-notes" placeholder="e.g. Weekly check / Guest checkout" style="padding: 6px 10px; font-size: 12px;">
                    </div>
                    <div style="display: flex; gap: 8px; margin-top: 10px;">
                        <button type="button" class="btn-quick" style="background: var(--primary); border: none; padding: 6px 12px; font-size: 12px; flex: 1;" onclick="submitMeterReading()">Save Reading</button>
                        <button type="button" class="btn-quick" style="background: #334155; border: none; padding: 6px 12px; font-size: 12px;" onclick="toggleMeterEntry()">Cancel</button>
                    </div>
                </div>
            </div>

            <!-- Export & Report Actions -->
            <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                <a href="/api/monitoring/export.csv" class="btn-quick" style="flex: 1; text-align: center; text-decoration: none; padding: 8px 12px; font-size: 12px; background: #0f172a; border: 1px solid var(--border); color: #38bdf8;" download="hvac_observations.csv">
                    📥 Download CSV Telemetry
                </a>
                <button type="button" class="btn-quick" style="flex: 1; padding: 8px 12px; font-size: 12px; background: #0f172a; border: 1px solid var(--border); color: #34d399;" onclick="viewObservationReport()">
                    📄 View 7-Day Report
                </button>
            </div>
        </div>

        <!-- Configuration Form -->
        <form id="config-form">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">

            <!-- System Operating Mode Card -->
            <div class="card">
                <div class="card-title" style="margin-bottom: 12px;">Automation Mode & Safeguards</div>
                <div class="form-field">
                    <label>System Operating Mode</label>
                    <select class="form-control" name="system_mode" id="cfg-system-mode">
                        <option value="AUTO">Automatic (Airbnb iCal Driven)</option>
                        <option value="MANUAL">Manual (Hold Fixed Target)</option>
                        <option value="PAUSED">Paused (Shut Down All AC Units)</option>
                    </select>
                </div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Heat During Blocked Dates</div>
                        <div class="toggle-sub">Treats Airbnb (Not available) dates as occupied stays for host/family</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="heat_during_blocked_dates" id="cfg-heat-blocked">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Preserve Guest Adjustments</div>
                        <div class="toggle-sub">Respect manual remote control temperature changes within each daypart</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="manual_override_preserve" id="cfg-manual-preserve">
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <!-- Adaptive Guest Comfort & Energy Optimization Card -->
            <div class="card" id="card-adaptive-comfort" style="border-left: 4px solid #10b981;">
                <div class="card-header" style="margin-bottom: 12px;">
                    <div>
                        <div class="card-title">✨ Adaptive Guest Comfort & Energy Policy</div>
                        <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">
                            Learns guest daytime preference and computes intelligent, dynamic night setbacks
                        </div>
                    </div>
                    <span id="badge-adaptive-comfort-status" class="badge badge-success">ACTIVE</span>
                </div>

                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Enable Adaptive Comfort Engine</div>
                        <div class="toggle-sub">Dynamically remembers guest daytime setpoints and applies relative night setback</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="adaptive_comfort_enabled" id="cfg-adaptive-enabled">
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Preserve Daytime Preferences Across Dayparts</div>
                        <div class="toggle-sub">Maintain guest remote changes across 07:00, 10:00, and 17:00 instead of snapping to profile</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="guest_preference_preservation" id="cfg-guest-pref-preserve">
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="form-row" style="margin-top: 14px;">
                    <div class="form-field">
                        <label>Night Setback Delta (°C)</label>
                        <input type="number" step="0.5" min="0.5" max="3.0" class="form-control" name="night_setback_delta" id="cfg-night-setback-delta">
                        <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">Subtracted from day preference (default: 1.0°C)</div>
                    </div>
                    <div class="form-field">
                        <label>Night Setback Floor (°C)</label>
                        <input type="number" step="0.5" min="16.0" max="22.0" class="form-control" name="night_setback_floor" id="cfg-night-setback-floor">
                        <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">Lowest automatic night temp (default: 18.0°C)</div>
                    </div>
                </div>

                <div class="form-row" style="margin-top: 10px;">
                    <div class="form-field">
                        <label>Economic Comfort Soft Limit (°C)</label>
                        <input type="number" step="0.5" min="20.0" max="26.0" class="form-control" name="economic_max_day_temp" id="cfg-economic-max-day-temp">
                        <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">Alerts on high heating setpoints (default: 24.0°C)</div>
                    </div>
                    <div class="form-field" style="display: flex; flex-direction: column; justify-content: center;">
                        <div class="toggle-label" style="font-size: 13px;">Economic Limit Enforcement</div>
                        <div class="toggle-sub" style="font-size: 11px; margin-bottom: 6px;">Nudge excessive setpoints back to soft limit</div>
                        <label class="switch">
                            <input type="checkbox" name="economic_enforcement_enabled" id="cfg-economic-enforcement">
                            <span class="slider"></span>
                        </label>
                    </div>
                </div>
            </div>

            <!-- Diurnal Temperatures Card -->
            <div class="card">
                <div class="card-header" style="margin-bottom: 12px;">
                    <div class="card-title">Scheduled Temperature Profiles</div>
                    <div style="display: flex; gap: 6px;">
                        <button type="button" class="btn-quick" style="font-size: 11px; padding: 4px 8px; min-height: 30px;" onclick="applyProfilePreset('SKI_DAY')">SKI_DAY (21°C Day)</button>
                        <button type="button" class="btn-quick" style="font-size: 11px; padding: 4px 8px; min-height: 30px;" onclick="applyProfilePreset('AT_HOME')">AT_HOME (23°C Day)</button>
                    </div>
                </div>
                <div class="form-field" style="margin-bottom: 14px;">
                    <label>Default Heating Profile</label>
                    <select class="form-control" name="default_heating_profile" id="cfg-default-profile">
                        <option value="SKI_DAY">SKI_DAY (Daytime setback to 21°C)</option>
                        <option value="AT_HOME">AT_HOME (Daytime warmth at 23°C)</option>
                        <option value="CUSTOM">CUSTOM (Editable Diurnal Setpoints)</option>
                    </select>
                </div>
                <div class="form-row">
                    <div class="form-field">
                        <label>Morning (07:00 – 10:00)</label>
                        <input type="number" step="0.5" min="16" max="30" class="form-control" name="schedule_morning_temp" id="cfg-temp-morning">
                    </div>
                    <div class="form-field">
                        <label>Morning Fan Speed</label>
                        <select class="form-control" name="schedule_morning_fan" id="cfg-fan-morning">
                            <option value="Auto">Auto</option>
                            <option value="Quiet">Quiet</option>
                            <option value="Low">Low</option>
                            <option value="Mid">Mid</option>
                            <option value="High">High</option>
                        </select>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-field">
                        <label>Day (10:00 – 17:00)</label>
                        <input type="number" step="0.5" min="16" max="30" class="form-control" name="schedule_day_temp" id="cfg-temp-day">
                    </div>
                    <div class="form-field">
                        <label>Day Fan Speed</label>
                        <select class="form-control" name="schedule_day_fan" id="cfg-fan-day">
                            <option value="Auto">Auto</option>
                            <option value="Quiet">Quiet</option>
                            <option value="Low">Low</option>
                            <option value="Mid">Mid</option>
                        </select>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-field">
                        <label>Evening (17:00 – 23:00)</label>
                        <input type="number" step="0.5" min="16" max="30" class="form-control" name="schedule_evening_temp" id="cfg-temp-evening">
                    </div>
                    <div class="form-field">
                        <label>Evening Fan Speed</label>
                        <select class="form-control" name="schedule_evening_fan" id="cfg-fan-evening">
                            <option value="Auto">Auto</option>
                            <option value="Mid">Mid</option>
                            <option value="High">High</option>
                        </select>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-field">
                        <label>Night (23:00 – 07:00)</label>
                        <input type="number" step="0.5" min="16" max="30" class="form-control" name="schedule_night_temp" id="cfg-temp-night">
                    </div>
                    <div class="form-field">
                        <label>Night Fan Speed</label>
                        <select class="form-control" name="schedule_night_fan" id="cfg-fan-night">
                            <option value="Quiet" selected>Quiet (Silent Night Mode)</option>
                            <option value="Low">Low</option>
                            <option value="Auto">Auto</option>
                        </select>
                    </div>
                </div>

                <!-- Independent Per-Room Diurnal Temperature Inputs -->
                <div style="margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border);">
                    <div style="font-weight: 600; font-size: 13px; margin-bottom: 6px; color: var(--text-main);">Independent Per-Room Setpoints</div>
                    <div style="font-size: 11px; color: var(--text-muted); margin-bottom: 10px;">
                        Configure independently editable diurnal temperatures for each room (e.g. Living Room 23°C / Bedroom 21°C daytime; both 20°C at night).
                    </div>
                    <div style="font-size: 12px; font-weight: 600; color: #60a5fa; margin-bottom: 6px;">🛋️ Living Room (demo_living_room)</div>
                    <div class="form-row">
                        <div class="form-field">
                            <label style="font-size: 11px;">LR Morning (07–10)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="living_room_morning_temp" id="cfg-lr-temp-morning">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">LR Day (10–17)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="living_room_day_temp" id="cfg-lr-temp-day">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">LR Evening (17–23)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="living_room_evening_temp" id="cfg-lr-temp-evening">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">LR Night (23–07)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="living_room_night_temp" id="cfg-lr-temp-night">
                        </div>
                    </div>
                    <div style="font-size: 12px; font-weight: 600; color: #a78bfa; margin-top: 8px; margin-bottom: 6px;">🛏️ Bedroom (demo_bedroom)</div>
                    <div class="form-row">
                        <div class="form-field">
                            <label style="font-size: 11px;">BR Morning (07–10)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="bedroom_morning_temp" id="cfg-br-temp-morning">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">BR Day (10–17)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="bedroom_day_temp" id="cfg-br-temp-day">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">BR Evening (17–23)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="bedroom_evening_temp" id="cfg-br-temp-evening">
                        </div>
                        <div class="form-field">
                            <label style="font-size: 11px;">BR Night (23–07)</label>
                            <input type="number" step="0.5" min="16" max="30" class="form-control" name="bedroom_night_temp" id="cfg-br-temp-night">
                        </div>
                    </div>
                </div>
            </div>

            <!-- Hardware Display & Audio Preferences Card -->
            <div class="card">
                <div class="card-title" style="margin-bottom: 12px;">Silent Night & Hardware Controls</div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Daytime LED Display (07:00 – 23:00)</div>
                        <div class="toggle-sub">Keep front-panel LED temperature displays visible for guests during the day (default: ON)</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="default_led_display" id="cfg-led-day">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Nighttime LED Display (23:00 – 07:00)</div>
                        <div class="toggle-sub">Dark screen mode at night so LEDs don't disturb sleeping guests (default: OFF)</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="night_led_display" id="cfg-led-night">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Audible Beep Feedback</div>
                        <div class="toggle-sub">Buzzer sound on command reception (kept OFF at all hours for silent unattended adjustments)</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="beep_feedback_enabled" id="cfg-beep">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="form-row" style="margin-top: 14px;">
                    <div class="form-field">
                        <label>Living Room LED Mode</label>
                        <select class="form-control" name="living_room_led_mode" id="cfg-lr-led-mode">
                            <option value="SCHEDULE">Follow Schedule (07:00–23:00 ON, 23:00–07:00 OFF)</option>
                            <option value="ALWAYS_OFF">Permanently Dark (Always OFF 24/7)</option>
                            <option value="ALWAYS_ON">Permanently Lit (Always ON when running)</option>
                        </select>
                    </div>
                    <div class="form-field">
                        <label>Bedroom LED Mode</label>
                        <select class="form-control" name="bedroom_led_mode" id="cfg-br-led-mode">
                            <option value="SCHEDULE">Follow Schedule (07:00–23:00 ON, 23:00–07:00 OFF)</option>
                            <option value="ALWAYS_OFF">Permanently Dark (Always OFF 24/7)</option>
                            <option value="ALWAYS_ON">Permanently Lit (Always ON when running)</option>
                        </select>
                    </div>
                </div>
            </div>

            <!-- Check-in / Checkout Timing & Rooms Card -->
            <div class="card">
                <div class="card-title" style="margin-bottom: 12px;">Property & Room Configurations</div>
                <div class="form-row">
                    <div class="form-field">
                        <label>Standard Check-in Time</label>
                        <input type="time" class="form-control" name="checkin_time" id="cfg-checkin-time">
                    </div>
                    <div class="form-field">
                        <label>Standard Checkout Time</label>
                        <input type="time" class="form-control" name="checkout_time" id="cfg-checkout-time">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-field">
                        <label>Pre-Arrival Heating Window</label>
                        <select class="form-control" name="pre_arrival_hours" id="cfg-pre-arrival-hours">
                            <option value="1">1 Hour Prior</option>
                            <option value="2">2 Hours Prior (Standard)</option>
                            <option value="3">3 Hours Prior</option>
                        </select>
                    </div>
                    <div class="form-field">
                        <label>Pre-Arrival Target Temp</label>
                        <input type="number" step="0.5" min="18" max="28" class="form-control" name="pre_arrival_temp" id="cfg-pre-arrival-temp">
                    </div>
                </div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Living Room AC Enabled</div>
                        <div class="toggle-sub">Enable autonomous automation for Living Room</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="living_room_enabled" id="cfg-lr-enabled">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="toggle-row">
                    <div>
                        <div class="toggle-label">Bedroom AC Enabled</div>
                        <div class="toggle-sub">Enable autonomous automation for Bedroom</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" name="bedroom_enabled" id="cfg-br-enabled">
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="form-row" style="margin-top: 10px;">
                    <div class="form-field">
                        <label>Living Room Offset (°C)</label>
                        <input type="number" step="0.5" min="-3" max="3" class="form-control" name="living_room_temp_offset" id="cfg-lr-offset">
                    </div>
                    <div class="form-field">
                        <label>Bedroom Offset (°C)</label>
                        <input type="number" step="0.5" min="-3" max="3" class="form-control" name="bedroom_temp_offset" id="cfg-br-offset">
                    </div>
                </div>
            </div>
        </form>

        <!-- Sticky Save Button -->
        <div class="sticky-footer">
            <button type="button" class="btn-save" onclick="saveConfiguration()">Save Changes to AWS Cloud</button>
        </div>
    </div>

    <script>
        const initialStatus = {_script_json(status_data)};
        const initialConfig = {_script_json(config_data)};
        const csrfToken = {_script_json(csrf_token)};

        let initialFormSnapshot = {{}};
        let configVersion = (initialConfig && initialConfig.version) ? initialConfig.version : 1;
        let configUpdatedAt = (initialConfig && initialConfig.updated_at) ? initialConfig.updated_at : null;
        let configEtag = (initialConfig && (initialConfig.etag || initialConfig._etag)) ? (initialConfig.etag || initialConfig._etag) : null;

        function getFormValues() {{
            const form = document.getElementById("config-form");
            if (!form) return {{}};
            const vals = {{
                system_mode: form.system_mode ? form.system_mode.value : "AUTO",
                heat_during_blocked_dates: form.heat_during_blocked_dates ? form.heat_during_blocked_dates.checked : true,
                manual_override_preserve: form.manual_override_preserve ? form.manual_override_preserve.checked : true,
                default_heating_profile: form.default_heating_profile ? form.default_heating_profile.value : "SKI_DAY",
                schedule_morning_temp: parseFloat(form.schedule_morning_temp.value),
                schedule_morning_fan: form.schedule_morning_fan.value,
                schedule_day_temp: parseFloat(form.schedule_day_temp.value),
                schedule_day_fan: form.schedule_day_fan.value,
                schedule_evening_temp: parseFloat(form.schedule_evening_temp.value),
                schedule_evening_fan: form.schedule_evening_fan.value,
                schedule_night_temp: parseFloat(form.schedule_night_temp.value),
                schedule_night_fan: form.schedule_night_fan.value,
                default_led_display: form.default_led_display ? form.default_led_display.checked : true,
                night_led_display: form.night_led_display ? form.night_led_display.checked : false,
                beep_feedback_enabled: form.beep_feedback_enabled ? form.beep_feedback_enabled.checked : false,
                living_room_led_mode: form.living_room_led_mode ? form.living_room_led_mode.value : "SCHEDULE",
                bedroom_led_mode: form.bedroom_led_mode ? form.bedroom_led_mode.value : "SCHEDULE",
                checkin_time: form.checkin_time.value,
                checkout_time: form.checkout_time.value,
                pre_arrival_hours: parseInt(form.pre_arrival_hours.value),
                pre_arrival_temp: parseFloat(form.pre_arrival_temp.value),
                living_room_enabled: form.living_room_enabled ? form.living_room_enabled.checked : true,
                bedroom_enabled: form.bedroom_enabled ? form.bedroom_enabled.checked : true,
                living_room_temp_offset: parseFloat(form.living_room_temp_offset ? form.living_room_temp_offset.value : 0.0),
                bedroom_temp_offset: parseFloat(form.bedroom_temp_offset.value),
                adaptive_comfort_enabled: form.adaptive_comfort_enabled ? form.adaptive_comfort_enabled.checked : true,
                guest_preference_preservation: form.guest_preference_preservation ? form.guest_preference_preservation.checked : true,
                night_setback_delta: form.night_setback_delta ? parseFloat(form.night_setback_delta.value) : 1.0,
                night_setback_floor: form.night_setback_floor ? parseFloat(form.night_setback_floor.value) : 18.0,
                economic_max_day_temp: form.economic_max_day_temp ? parseFloat(form.economic_max_day_temp.value) : 24.0,
                economic_enforcement_enabled: form.economic_enforcement_enabled ? form.economic_enforcement_enabled.checked : false
            }};

            if (form.living_room_morning_temp && form.living_room_morning_temp.value) vals.living_room_morning_temp = parseFloat(form.living_room_morning_temp.value);
            if (form.living_room_day_temp && form.living_room_day_temp.value) vals.living_room_day_temp = parseFloat(form.living_room_day_temp.value);
            if (form.living_room_evening_temp && form.living_room_evening_temp.value) vals.living_room_evening_temp = parseFloat(form.living_room_evening_temp.value);
            if (form.living_room_night_temp && form.living_room_night_temp.value) vals.living_room_night_temp = parseFloat(form.living_room_night_temp.value);

            if (form.bedroom_morning_temp && form.bedroom_morning_temp.value) vals.bedroom_morning_temp = parseFloat(form.bedroom_morning_temp.value);
            if (form.bedroom_day_temp && form.bedroom_day_temp.value) vals.bedroom_day_temp = parseFloat(form.bedroom_day_temp.value);
            if (form.bedroom_evening_temp && form.bedroom_evening_temp.value) vals.bedroom_evening_temp = parseFloat(form.bedroom_evening_temp.value);
            if (form.bedroom_night_temp && form.bedroom_night_temp.value) vals.bedroom_night_temp = parseFloat(form.bedroom_night_temp.value);

            return vals;
        }}

        function showToast(msg, isError = false) {{
            const toast = document.getElementById("toast");
            toast.innerText = msg;
            toast.style.backgroundColor = isError ? "#ef4444" : "#10b981";
            toast.className = "show";
            setTimeout(() => {{ toast.className = toast.className.replace("show", ""); }}, 3000);
        }}

        let currentStayUid = null;
        let upcomingStayUid = null;

        function applyProfilePreset(name) {{
            const profSelect = document.getElementById("cfg-default-profile");
            if (profSelect) profSelect.value = name;

            if (name === "AT_HOME") {{
                document.getElementById("cfg-temp-morning").value = 23.0;
                document.getElementById("cfg-fan-morning").value = "Mid";
                document.getElementById("cfg-temp-day").value = 23.0;
                document.getElementById("cfg-fan-day").value = "Mid";
                document.getElementById("cfg-temp-evening").value = 23.0;
                document.getElementById("cfg-fan-evening").value = "Mid";
                document.getElementById("cfg-temp-night").value = 20.0;
                document.getElementById("cfg-fan-night").value = "Quiet";

                const lrM = document.getElementById("cfg-lr-temp-morning"); if (lrM) lrM.value = 23.0;
                const lrD = document.getElementById("cfg-lr-temp-day"); if (lrD) lrD.value = 23.0;
                const lrE = document.getElementById("cfg-lr-temp-evening"); if (lrE) lrE.value = 23.0;
                const lrN = document.getElementById("cfg-lr-temp-night"); if (lrN) lrN.value = 20.0;

                const brM = document.getElementById("cfg-br-temp-morning"); if (brM) brM.value = 22.0;
                const brD = document.getElementById("cfg-br-temp-day"); if (brD) brD.value = 21.0;
                const brE = document.getElementById("cfg-br-temp-evening"); if (brE) brE.value = 22.0;
                const brN = document.getElementById("cfg-br-temp-night"); if (brN) brN.value = 20.0;

                showToast("Applied AT_HOME preset (LR 23°C / BR 21°C Day, 20°C Night)");
            }} else if (name === "SKI_DAY") {{
                document.getElementById("cfg-temp-morning").value = 23.0;
                document.getElementById("cfg-fan-morning").value = "Mid";
                document.getElementById("cfg-temp-day").value = 21.0;
                document.getElementById("cfg-fan-day").value = "Auto";
                document.getElementById("cfg-temp-evening").value = 23.0;
                document.getElementById("cfg-fan-evening").value = "Mid";
                document.getElementById("cfg-temp-night").value = 20.0;
                document.getElementById("cfg-fan-night").value = "Quiet";

                const lrM = document.getElementById("cfg-lr-temp-morning"); if (lrM) lrM.value = 23.0;
                const lrD = document.getElementById("cfg-lr-temp-day"); if (lrD) lrD.value = 21.0;
                const lrE = document.getElementById("cfg-lr-temp-evening"); if (lrE) lrE.value = 23.0;
                const lrN = document.getElementById("cfg-lr-temp-night"); if (lrN) lrN.value = 20.0;

                const brM = document.getElementById("cfg-br-temp-morning"); if (brM) brM.value = 22.0;
                const brD = document.getElementById("cfg-br-temp-day"); if (brD) brD.value = 20.0;
                const brE = document.getElementById("cfg-br-temp-evening"); if (brE) brE.value = 22.0;
                const brN = document.getElementById("cfg-br-temp-night"); if (brN) brN.value = 20.0;

                showToast("Applied SKI_DAY preset (Eco 21°C Day)");
            }}
        }}

        async function saveStayProfile() {{
            const uid = currentStayUid || upcomingStayUid;
            if (!uid) {{
                showToast("No active or upcoming stay available to configure", true);
                return;
            }}
            const prof = document.getElementById("res-profile-select").value;
            try {{
                const res = await fetch("/api/reservation-profile", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRF-Token": csrfToken
                    }},
                    body: JSON.stringify({{
                        csrf_token: csrfToken,
                        uid: uid,
                        profile: prof
                    }})
                }});
                const data = await res.json();
                if (res.ok && data.status === "success") {{
                    showToast("Reservation profile assigned: " + prof);
                    const updated = await (await fetch("/api/status")).json();
                    updateStatusUI(updated);
                }} else {{
                    showToast(data.error || "Failed to assign stay profile", true);
                }}
            }} catch (err) {{
                showToast("Network error saving stay profile", true);
            }}
        }}

        function populateForm(cfg) {{
            document.getElementById("cfg-system-mode").value = cfg.system_mode || "AUTO";
            document.getElementById("cfg-heat-blocked").checked = cfg.heat_during_blocked_dates !== false;
            document.getElementById("cfg-manual-preserve").checked = cfg.manual_override_preserve !== false;

            const defProfElem = document.getElementById("cfg-default-profile");
            if (defProfElem) defProfElem.value = cfg.default_heating_profile || "SKI_DAY";

            document.getElementById("cfg-temp-morning").value = cfg.schedule_morning_temp || 23.0;
            document.getElementById("cfg-fan-morning").value = cfg.schedule_morning_fan || "Mid";

            document.getElementById("cfg-temp-day").value = cfg.schedule_day_temp || 21.0;
            document.getElementById("cfg-fan-day").value = cfg.schedule_day_fan || "Auto";

            document.getElementById("cfg-temp-evening").value = cfg.schedule_evening_temp || 23.0;
            document.getElementById("cfg-fan-evening").value = cfg.schedule_evening_fan || "Mid";

            document.getElementById("cfg-temp-night").value = cfg.schedule_night_temp || 20.0;
            document.getElementById("cfg-fan-night").value = cfg.schedule_night_fan || "Quiet";

            document.getElementById("cfg-led-day").checked = cfg.default_led_display !== false;
            document.getElementById("cfg-led-night").checked = !!cfg.night_led_display;
            document.getElementById("cfg-beep").checked = !!cfg.beep_feedback_enabled;
            document.getElementById("cfg-lr-led-mode").value = cfg.living_room_led_mode || "SCHEDULE";
            document.getElementById("cfg-br-led-mode").value = cfg.bedroom_led_mode || "SCHEDULE";

            document.getElementById("cfg-checkin-time").value = cfg.checkin_time || "15:00";
            document.getElementById("cfg-checkout-time").value = cfg.checkout_time || "11:00";
            document.getElementById("cfg-pre-arrival-hours").value = cfg.pre_arrival_hours || 2;
            document.getElementById("cfg-pre-arrival-temp").value = cfg.pre_arrival_temp || 23.0;

            document.getElementById("cfg-lr-enabled").checked = cfg.living_room_enabled !== false;
            document.getElementById("cfg-br-enabled").checked = cfg.bedroom_enabled !== false;
            const lrOffElem = document.getElementById("cfg-lr-offset");
            if (lrOffElem) lrOffElem.value = cfg.living_room_temp_offset || 0.0;
            document.getElementById("cfg-br-offset").value = cfg.bedroom_temp_offset || 0.0;

            const lrM = document.getElementById("cfg-lr-temp-morning"); if (lrM) lrM.value = cfg.living_room_morning_temp != null ? cfg.living_room_morning_temp : (cfg.schedule_morning_temp || 23.0);
            const lrD = document.getElementById("cfg-lr-temp-day"); if (lrD) lrD.value = cfg.living_room_day_temp != null ? cfg.living_room_day_temp : (cfg.schedule_day_temp || 23.0);
            const lrE = document.getElementById("cfg-lr-temp-evening"); if (lrE) lrE.value = cfg.living_room_evening_temp != null ? cfg.living_room_evening_temp : (cfg.schedule_evening_temp || 23.0);
            const lrN = document.getElementById("cfg-lr-temp-night"); if (lrN) lrN.value = cfg.living_room_night_temp != null ? cfg.living_room_night_temp : (cfg.schedule_night_temp || 20.0);

            const brM = document.getElementById("cfg-br-temp-morning"); if (brM) brM.value = cfg.bedroom_morning_temp != null ? cfg.bedroom_morning_temp : 22.0;
            const brD = document.getElementById("cfg-br-temp-day"); if (brD) brD.value = cfg.bedroom_day_temp != null ? cfg.bedroom_day_temp : 21.0;
            const brE = document.getElementById("cfg-br-temp-evening"); if (brE) brE.value = cfg.bedroom_evening_temp != null ? cfg.bedroom_evening_temp : 22.0;
            const brN = document.getElementById("cfg-br-temp-night"); if (brN) brN.value = cfg.bedroom_night_temp != null ? cfg.bedroom_night_temp : 20.0;

            const adaptElem = document.getElementById("cfg-adaptive-enabled");
            if (adaptElem) adaptElem.checked = cfg.adaptive_comfort_enabled !== false;
            const guestPrefElem = document.getElementById("cfg-guest-pref-preserve");
            if (guestPrefElem) guestPrefElem.checked = cfg.guest_preference_preservation !== false;
            const nightDeltaElem = document.getElementById("cfg-night-setback-delta");
            if (nightDeltaElem) nightDeltaElem.value = cfg.night_setback_delta != null ? cfg.night_setback_delta : 1.0;
            const nightFloorElem = document.getElementById("cfg-night-setback-floor");
            if (nightFloorElem) nightFloorElem.value = cfg.night_setback_floor != null ? cfg.night_setback_floor : 18.0;
            const econTempElem = document.getElementById("cfg-economic-max-day-temp");
            if (econTempElem) econTempElem.value = cfg.economic_max_day_temp != null ? cfg.economic_max_day_temp : 24.0;
            const econEnfElem = document.getElementById("cfg-economic-enforcement");
            if (econEnfElem) econEnfElem.checked = cfg.economic_enforcement_enabled === true;
        }}

        function updateStatusUI(st) {{
            if (!st) return;

            // Safety Banner
            const banner = document.getElementById("safety-banner");
            const sIcon = document.getElementById("safety-icon");
            const sLabel = document.getElementById("safety-label");
            const sBadge = document.getElementById("safety-badge");
            if (banner && sIcon && sLabel && sBadge) {{
                if (st.read_only) {{
                    banner.style.background = "rgba(59, 130, 246, 0.12)";
                    banner.style.borderColor = "#2563eb";
                    sIcon.innerText = "🛡️";
                    sLabel.innerText = "READ_ONLY MODE: Commands suppressed; read-only telemetry monitoring";
                    sBadge.innerText = "READ ONLY";
                    sBadge.className = "badge badge-info";
                }} else if (st.dry_run) {{
                    banner.style.background = "rgba(245, 158, 11, 0.12)";
                    banner.style.borderColor = "#d97706";
                    sIcon.innerText = "🛡️";
                    sLabel.innerText = "DRY_RUN ACTIVE: Automation computes decisions safely without modifying physical ACs";
                    sBadge.innerText = "DRY RUN";
                    sBadge.className = "badge badge-warning";
                }} else {{
                    banner.style.background = "rgba(16, 185, 129, 0.12)";
                    banner.style.borderColor = "#059669";
                    sIcon.innerText = "⚡";
                    sLabel.innerText = "LIVE PRODUCTION: Autonomous control commands are actively dispatched";
                    sBadge.innerText = "LIVE";
                    sBadge.className = "badge badge-success";
                }}
            }}

            // Occupancy Status Card
            if (st.occupancy) {{
                const occ = st.occupancy;
                const obadge = document.getElementById("badge-occupancy-status");
                if (obadge) {{
                    obadge.innerText = occ.status || "STANDBY";
                    if (occ.status === "GUEST OCCUPIED" || occ.status === "CONFIRMED GUEST") {{
                        obadge.className = "badge badge-success";
                    }} else if (occ.status === "OWNER PRESENT") {{
                        obadge.className = "badge badge-warning";
                    }} else if (occ.status === "BLOCKED DATES" || occ.status === "OWNER TEST MODE") {{
                        obadge.className = "badge badge-warning";
                    }} else if (occ.status === "MANUAL HOLD") {{
                        obadge.className = "badge badge-info";
                    }} else if (occ.status === "UNOCCUPIED" || occ.status === "VACANT / STANDBY") {{
                        obadge.className = "badge badge-info";
                    }} else if (occ.status === "SYSTEM PAUSED") {{
                        obadge.className = "badge badge-danger";
                    }} else {{
                        obadge.className = "badge badge-info";
                    }}
                }}
                const odesc = document.getElementById("occupancy-desc-text");
                if (odesc) odesc.innerText = occ.description || "";

                const pbadge = document.getElementById("badge-active-profile");
                if (pbadge && st.active_profile) {{
                    pbadge.innerText = st.active_profile;
                    pbadge.className = (st.active_profile === "AT_HOME") ? "badge badge-warning" : "badge badge-info";
                }}

                // Active or Upcoming Stay Profile Selector
                const resBox = document.getElementById("reservation-profile-box");
                if (resBox) {{
                    const stay = occ.current_stay || occ.upcoming_stay;
                    if (stay && stay.uid) {{
                        currentStayUid = occ.current_stay ? occ.current_stay.uid : null;
                        upcomingStayUid = occ.upcoming_stay ? occ.upcoming_stay.uid : null;
                        resBox.style.display = "block";
                        const titleElem = document.getElementById("res-profile-title");
                        if (titleElem) {{
                            const prefix = occ.current_stay ? "Active Stay (" : "Upcoming Stay (";
                            titleElem.innerText = prefix + stay.uid_masked + "):";
                        }}
                        const currElem = document.getElementById("res-profile-current");
                        if (currElem) currElem.innerText = stay.assigned_profile || st.active_profile || "SKI_DAY";
                        const selectElem = document.getElementById("res-profile-select");
                        if (selectElem) selectElem.value = stay.assigned_profile || "SKI_DAY";
                    }} else {{
                        resBox.style.display = "none";
                    }}
                }}

                if (occ.counts) {{
                    const confElem = document.getElementById("occ-confirmed-count");
                    if (confElem) confElem.innerText = occ.counts.confirmed || 0;
                    const blkElem = document.getElementById("occ-blocked-count");
                    if (blkElem) blkElem.innerText = occ.counts.blocked || 0;
                }}
                const upElem = document.getElementById("occ-upcoming-date");
                if (upElem) {{
                    if (occ.upcoming_stay) {{
                        const typeLabel = occ.upcoming_stay.type === "CONFIRMED" ? "Guest" : "Blocked";
                        upElem.innerText = occ.upcoming_stay.start_date + " (" + typeLabel + ")";
                    }} else {{
                        upElem.innerText = "None";
                    }}
                }}
                const staleElem = document.getElementById("occ-calendar-freshness");
                if (staleElem) {{
                    if (occ.calendar_stale) {{
                        staleElem.innerText = "Stale (>4h)";
                        staleElem.style.color = "#f87171";
                    }} else {{
                        staleElem.innerText = "Active / Fresh";
                        staleElem.style.color = "#34d399";
                    }}
                }}
            }}

            const badge = document.getElementById("badge-system-state");
            if (badge) {{
                badge.innerText = st.system_state || "UNKNOWN";
                if (st.system_state === "PAUSED") {{
                    badge.className = "badge badge-danger";
                }} else if (st.system_state && st.system_state.includes("OCCUPIED")) {{
                    badge.className = "badge badge-success";
                }} else if (st.system_state === "PRE_ARRIVAL_HEATING" || st.system_state === "OWNER_TEST_HOLD") {{
                    badge.className = "badge badge-warning";
                }} else {{
                    badge.className = "badge badge-info";
                }}
            }}

            const reasonElem = document.getElementById("status-reason-text");
            if (reasonElem) reasonElem.innerText = st.decision_reason || "Normal operation";

            // Telemetry Freshness & Timestamp Display
            if (st.telemetry_timestamp) {{
                const timeElem = document.getElementById("telemetry-timestamp-text");
                if (timeElem) timeElem.innerText = st.telemetry_timestamp + (st.telemetry_age_str ? " (" + st.telemetry_age_str + ")" : "");
            }}
            const staleAlert = document.getElementById("stale-telemetry-alert");
            const staleAge = document.getElementById("stale-telemetry-age");
            if (staleAlert) {{
                if (st.telemetry_is_stale) {{
                    staleAlert.style.display = "block";
                    if (staleAge) staleAge.innerText = st.telemetry_age_str || "over 20 mins ago";
                }} else {{
                    staleAlert.style.display = "none";
                }}
            }}

            // Owner Notification Status
            const notif = st.notifications || {{}};
            const notifBadge = document.getElementById("badge-notif-status");
            const notifChannel = document.getElementById("notif-channel-text");
            const notifConfigured = document.getElementById("notif-configured-text");
            const notifLast = document.getElementById("notif-last-delivery-text");
            const notifErrBox = document.getElementById("notif-error-box");
            const notifErrText = document.getElementById("notif-error-text");

            if (notifBadge) {{
                const status = notif.status || st.notification_status || "NOT CONFIGURED";
                notifBadge.innerText = status;
                if (status === "LAST DELIVERY SUCCESSFUL") {{
                    notifBadge.className = "badge badge-success";
                }} else if (status === "CONFIGURED — UNTESTED") {{
                    notifBadge.className = "badge badge-warning";
                }} else if (status === "DELIVERY FAILED") {{
                    notifBadge.className = "badge badge-danger";
                }} else {{
                    notifBadge.className = "badge badge-dimmed";
                }}
            }}
            if (notifChannel) {{
                notifChannel.innerText = notif.channel || st.notification_channel || "None";
            }}
            if (notifConfigured) {{
                notifConfigured.innerText = notif.configured ? "YES" : "NO";
                notifConfigured.style.color = notif.configured ? "#34d399" : "#94a3b8";
            }}
            if (notifLast) {{
                notifLast.innerText = notif.last_delivery_timestamp_sofia || notif.last_delivery_timestamp || "Never";
            }}
            if (notifErrBox && notifErrText) {{
                if ((notif.status === "DELIVERY FAILED") && notif.last_delivery_error) {{
                    notifErrBox.style.display = "block";
                    notifErrText.innerText = notif.last_delivery_error;
                }} else {{
                    notifErrBox.style.display = "none";
                }}
            }}

            // Device UI updater helper
            function updateDeviceBox(prefix, dev, telemetryStale) {{
                if (!dev) return;

                const idElem = document.getElementById(prefix + "-device-id");
                if (idElem) idElem.innerText = dev.device_id || "";

                // Online status badge
                const onlineBadge = document.getElementById(prefix + "-online-badge");
                if (onlineBadge) {{
                    if (dev.raw_online === false) {{
                        onlineBadge.innerText = "OFFLINE";
                        onlineBadge.className = "badge badge-danger";
                    }} else if (telemetryStale || dev.telemetry_is_stale) {{
                        onlineBadge.innerText = "STALE";
                        onlineBadge.className = "badge badge-warning";
                    }} else if (dev.online) {{
                        onlineBadge.innerText = "ONLINE";
                        onlineBadge.className = "badge badge-success";
                    }} else {{
                        onlineBadge.innerText = "OFFLINE";
                        onlineBadge.className = "badge badge-danger";
                    }}
                }}

                // Physical Power badge
                const pbadge = document.getElementById(prefix + "-power-badge");
                if (pbadge) {{
                    const pwr = dev.power;
                    pbadge.innerText = pwr ? "POWER ON" : "POWER OFF";
                    pbadge.className = pwr ? "badge badge-success" : "badge badge-danger";
                }}

                // Guest Power-OFF Override badge
                const guestOffBadge = document.getElementById(prefix + "-guest-override-badge");
                if (guestOffBadge) {{
                    if (dev.guest_power_off_override) {{
                        guestOffBadge.style.display = "inline-block";
                    }} else {{
                        guestOffBadge.style.display = "none";
                    }}
                }}

                // Freshness Badge & Measured Temp
                const freshBadge = document.getElementById(prefix + "-temp-freshness-badge");
                const tAmb = document.getElementById(prefix + "-ambient-temp");
                const tAge = document.getElementById(prefix + "-temp-age");
                const freshness = dev.temp_freshness || (dev.current_temperature != null ? (telemetryStale ? "STALE" : "FRESH") : "UNAVAILABLE");

                if (freshBadge) {{
                    if (freshness === "FRESH") {{
                        freshBadge.innerText = "FRESH";
                        freshBadge.className = "badge badge-success";
                    }} else if (freshness === "STALE") {{
                        freshBadge.innerText = "STALE";
                        freshBadge.className = "badge badge-warning";
                    }} else {{
                        freshBadge.innerText = "UNAVAILABLE";
                        freshBadge.className = "badge badge-dimmed";
                    }}
                }}

                if (tAmb) {{
                    if (dev.current_temperature != null) {{
                        tAmb.innerText = dev.current_temperature + "°C";
                        if (freshness === "STALE") {{
                            tAmb.style.color = "#fbbf24";
                        }} else if (freshness === "UNAVAILABLE") {{
                            tAmb.style.color = "#94a3b8";
                        }} else {{
                            tAmb.style.color = "#38bdf8";
                        }}
                    }} else {{
                        tAmb.innerText = "N/A";
                        tAmb.style.color = "#94a3b8";
                    }}
                }}

                if (tAge) {{
                    if (freshness === "FRESH") {{
                        tAge.innerText = "(measured " + (dev.temp_age_str || "just now") + ")";
                        tAge.style.color = "var(--text-muted)";
                    }} else if (freshness === "STALE") {{
                        const timeStr = dev.temp_measured_at_sofia ? " at " + dev.temp_measured_at_sofia : "";
                        tAge.innerText = "⚠️ measured " + (dev.temp_age_str || "stale") + timeStr;
                        tAge.style.color = "#fbbf24";
                    }} else {{
                        tAge.innerText = "(timestamp unavailable)";
                        tAge.style.color = "#64748b";
                    }}
                }}

                // Target Setpoint
                const tTarget = document.getElementById(prefix + "-target-temp");
                if (tTarget) tTarget.innerText = (dev.target_temperature != null ? dev.target_temperature : "--") + "°C";

                // Device mode and fan
                const mElem = document.getElementById(prefix + "-mode");
                if (mElem) mElem.innerText = dev.mode || "Heat";
                const fElem = document.getElementById(prefix + "-fan");
                if (fElem) fElem.innerText = dev.fan_speed || "Auto";
                const lElem = document.getElementById(prefix + "-led");
                if (lElem) lElem.innerText = dev.display_led ? "ON" : "OFF";
                const bElem = document.getElementById(prefix + "-beep");
                if (bElem) bElem.innerText = dev.beep_feedback ? "ON" : "OFF";

                // Remote override alert
                const overrideAlert = document.getElementById(prefix + "-override-alert");
                const overrideLabel = document.getElementById(prefix + "-override-label");
                const overrideText = document.getElementById(prefix + "-override-text");
                if (overrideAlert && overrideText) {{
                    if (dev.override_active) {{
                        overrideAlert.style.display = "block";
                        if (overrideLabel) overrideLabel.innerText = (dev.override_type || "MANUAL OVERRIDE") + ":";
                        overrideText.innerText = dev.override_details || "Manual adjustment active";
                    }} else {{
                        overrideAlert.style.display = "none";
                    }}
                }}

                // Active Warnings Box
                const warnBox = document.getElementById(prefix + "-warnings-box");
                const warnText = document.getElementById(prefix + "-warnings-text");
                if (warnBox && warnText) {{
                    if (dev.active_warnings && dev.active_warnings.length > 0) {{
                        warnBox.style.display = "block";
                        const isSevere = dev.active_warnings.some(function(w) {{ return w.indexOf("CRITICAL") !== -1 || w.indexOf("OFFLINE") !== -1; }});
                        if (isSevere) {{
                            warnBox.style.background = "rgba(239, 68, 68, 0.15)";
                            warnBox.style.borderColor = "#dc2626";
                            warnBox.style.color = "#fca5a5";
                        }} else {{
                            warnBox.style.background = "rgba(245, 158, 11, 0.15)";
                            warnBox.style.borderColor = "#d97706";
                            warnBox.style.color = "#fbbf24";
                        }}
                        warnText.textContent = dev.active_warnings.map(function(w) {{ return "⚠️ " + w; }}).join("\n");
                    }} else {{
                        warnBox.style.display = "none";
                    }}
                }}
            }}

            // Living Room & Bedroom Boxes
            if (st.devices) {{
                if (st.devices.living_room) updateDeviceBox("lr", st.devices.living_room, st.telemetry_is_stale);
                if (st.devices.bedroom) updateDeviceBox("br", st.devices.bedroom, st.telemetry_is_stale);
            }}

            // Adaptive Comfort Telemetry Update
            if (st.adaptive_comfort) {{
                const ac = st.adaptive_comfort;
                const acBadge = document.getElementById("live-adaptive-status-badge");
                if (acBadge) {{
                    acBadge.innerText = ac.enabled ? "ACTIVE" : "DISABLED";
                    acBadge.className = ac.enabled ? "badge badge-success" : "badge badge-info";
                }}
                const formBadge = document.getElementById("badge-adaptive-comfort-status");
                if (formBadge) {{
                    formBadge.innerText = ac.enabled ? "ACTIVE" : "DISABLED";
                    formBadge.className = ac.enabled ? "badge badge-success" : "badge badge-info";
                }}
                const lrPref = document.getElementById("live-lr-pref-temp");
                if (lrPref) {{
                    lrPref.innerText = ac.living_room_preferred_day_temp != null ? ac.living_room_preferred_day_temp + "°C" : "Default";
                }}
                const brPref = document.getElementById("live-br-pref-temp");
                if (brPref) {{
                    brPref.innerText = ac.bedroom_preferred_day_temp != null ? ac.bedroom_preferred_day_temp + "°C" : "Default";
                }}
                const brLock = document.getElementById("live-br-power-lock");
                if (brLock) {{
                    brLock.innerText = ac.bedroom_guest_power_off ? "Guest Manual OFF (Locked)" : "Unlocked";
                    brLock.style.color = ac.bedroom_guest_power_off ? "#f87171" : "#34d399";
                }}
                const lastTrans = document.getElementById("live-last-transitions");
                if (lastTrans && ac.last_transitions) {{
                    const parts = [];
                    if (ac.last_transitions.morning) parts.push("07:00 (" + ac.last_transitions.morning + ")");
                    if (ac.last_transitions.evening) parts.push("17:00 (" + ac.last_transitions.evening + ")");
                    if (ac.last_transitions.night) parts.push("23:00 (" + ac.last_transitions.night + ")");
                    lastTrans.innerText = parts.length > 0 ? parts.join(", ") : "None recorded";
                }}
            }}
            if (st.owner_test && st.owner_test.active) {{
                const ot = st.owner_test;
                const isOwner = (ot.session_type === "OWNER_PRESENT");
                document.getElementById("owner-test-status-badge").innerText = isOwner ? "ACTIVE (OWNER)" : "ACTIVE (TEST)";
                document.getElementById("owner-test-status-badge").className = "badge badge-warning";
                document.getElementById("owner-test-active-view").style.display = "block";
                document.getElementById("owner-test-setup-view").style.display = "none";

                const headElem = document.getElementById("owner-test-heading");
                if (headElem) headElem.innerText = isOwner ? "🏡 Owner Presence Active" : "🔧 Diagnostic Test Setpoint";

                const countElem = document.getElementById("owner-test-countdown");
                if (countElem) countElem.innerText = "Remaining: " + (ot.remaining_str || "--");

                const expElem = document.getElementById("owner-test-expiry-text");
                if (expElem) expElem.innerText = "Expires: " + (ot.expires_at_formatted || ot.expires_at || "scheduled time");

                const detElem = document.getElementById("owner-test-details");
                if (detElem) {{
                    if (isOwner) {{
                        detElem.innerText = "Following diurnal heating schedule (LR 23°C / BR 21°C Day, 20°C Night Setback, Quiet fan, Screen OFF).";
                    }} else {{
                        detElem.innerText = "Holding fixed setpoint " + ot.target_temperature + "°C (" + ot.mode + ").";
                    }}
                }}

                // Expiring soon warning (< 2 hours remaining)
                const alertElem = document.getElementById("owner-test-expiring-alert");
                const timeElem = document.getElementById("owner-test-expiring-time");
                if (alertElem) {{
                    if (ot.is_expiring_soon || (ot.remaining_seconds != null && ot.remaining_seconds > 0 && ot.remaining_seconds <= 7200)) {{
                        alertElem.style.display = "block";
                        if (timeElem) timeElem.innerText = ot.remaining_str || "under 2 hours";
                    }} else {{
                        alertElem.style.display = "none";
                    }}
                }}
            }} else {{
                document.getElementById("owner-test-status-badge").innerText = "STANDBY";
                document.getElementById("owner-test-status-badge").className = "badge badge-info";
                document.getElementById("owner-test-active-view").style.display = "none";
                document.getElementById("owner-test-setup-view").style.display = "block";
                const alertElem = document.getElementById("owner-test-expiring-alert");
                if (alertElem) alertElem.style.display = "none";
            }}

            // 7-Day HVAC Observation & Energy Monitoring Update
            const mon = st.monitoring || {{}};
            const monBadge = document.getElementById("badge-monitoring-status");
            if (monBadge) {{
                monBadge.innerText = mon.enabled ? "RECORDING" : "DISABLED";
                monBadge.className = mon.enabled ? "badge badge-success" : "badge badge-dimmed";
            }}
            const lrOnElem = document.getElementById("mon-lr-on-today");
            if (lrOnElem) {{
                lrOnElem.innerText = (mon.living_room_approx_on_hours_today != null ? mon.living_room_approx_on_hours_today : 0.0) + "h";
            }}
            const brOnElem = document.getElementById("mon-br-on-today");
            if (brOnElem) {{
                brOnElem.innerText = (mon.bedroom_approx_on_hours_today != null ? mon.bedroom_approx_on_hours_today : 0.0) + "h";
            }}
            const wElem = document.getElementById("mon-weather-today");
            if (wElem) {{
                if (mon.weather_today && mon.weather_today.avg_temp_c != null) {{
                    wElem.innerText = mon.weather_today.avg_temp_c + "°C";
                }} else {{
                    wElem.innerText = "--°C";
                }}
            }}
            const evBox = document.getElementById("mon-recent-events");
            if (evBox) {{
                if (mon.recent_events && mon.recent_events.length > 0) {{
                    evBox.textContent = mon.recent_events.map(function(ev) {{
                        const timeStr = ev.timestamp_sofia ? (ev.timestamp_sofia.split(", ")[1] || ev.timestamp_sofia) : "";
                        const roomLabel = ev.room_name === "bedroom" ? "Bedroom" : "Living Room";
                        return timeStr + ' ' + roomLabel + ': ' + (ev.event_type || '') + (ev.details ? ' (' + ev.details + ')' : '');
                    }}).join("\n");
                }} else {{
                    evBox.textContent = 'No state changes recorded today.';
                }}
            }}
            const meterElem = document.getElementById("mon-latest-meter");
            if (meterElem) {{
                if (mon.meter_readings && mon.meter_readings.length > 0) {{
                    const latest = mon.meter_readings[mon.meter_readings.length - 1];
                    meterElem.textContent = 'Latest Reading: ' + latest.total_kwh + ' kWh (' + (latest.recorded_at_sofia || '') + ' by ' + (latest.recorded_by || 'Owner') + ')';
                }} else {{
                    meterElem.textContent = 'Latest Reading: None recorded';
                }}
            }}
        }}

        async function saveConfiguration() {{
            const currentVals = getFormValues();
            const dirtyDiff = {{}};
            let hasChanges = false;

            for (const [key, val] of Object.entries(currentVals)) {{
                if (initialFormSnapshot[key] !== val) {{
                    dirtyDiff[key] = val;
                    hasChanges = true;
                }}
            }}

            if (!hasChanges) {{
                showToast("No changes detected to save");
                return;
            }}

            // Include optimistic concurrency metadata
            dirtyDiff.base_version = configVersion;
            dirtyDiff.base_updated_at = configUpdatedAt;
            if (configEtag) dirtyDiff.base_etag = configEtag;

            try {{
                const res = await fetch("/api/config", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRF-Token": csrfToken
                    }},
                    body: JSON.stringify(dirtyDiff)
                }});
                const data = await res.json();
                if (res.status === 409) {{
                    showToast(data.error || "Conflict: Configuration changed elsewhere. Please reload.", true);
                    return;
                }}
                if (res.ok && data.status === "success") {{
                    showToast("Configuration saved to AWS S3!");
                    initialFormSnapshot = getFormValues();
                    if (data.config) {{
                        if (data.config.version) configVersion = data.config.version;
                        if (data.config.updated_at) configUpdatedAt = data.config.updated_at;
                        if (data.config.etag || data.config._etag) configEtag = (data.config.etag || data.config._etag);
                    }}
                }} else {{
                    showToast(data.error || "Failed to save settings", true);
                }}
            }} catch (err) {{
                showToast("Network error saving configuration", true);
            }}
        }}

        function toggleOwnerSessionType() {{
            const stype = document.getElementById("owner-session-type").value;
            const tempField = document.getElementById("owner-test-temp-field");
            if (tempField) {{
                tempField.style.display = (stype === "OWNER_TEST") ? "block" : "none";
            }}
        }}

        async function startOwnerTest() {{
            const stype = document.getElementById("owner-session-type") ? document.getElementById("owner-session-type").value : "OWNER_PRESENT";
            const dur = parseFloat(document.getElementById("owner-test-duration").value);
            const temp = parseFloat(document.getElementById("owner-test-temp").value);

            try {{
                const res = await fetch("/api/owner-test", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRF-Token": csrfToken
                    }},
                    body: JSON.stringify({{
                        action: "START",
                        session_type: stype,
                        duration_hours: dur,
                        target_temperature: temp,
                        mode: "Heat"
                    }})
                }});
                const data = await res.json();
                if (res.ok && data.status === "success") {{
                    showToast(stype === "OWNER_PRESENT" ? "Owner Presence Activated (Diurnal Schedule)!" : "Diagnostic Test Mode Activated!");
                    const updated = await (await fetch("/api/status")).json();
                    updateStatusUI(updated);
                }} else {{
                    showToast(data.error || "Failed to activate owner mode", true);
                }}
            }} catch (err) {{
                showToast("Network error", true);
            }}
        }}

        async function extendOwnerSession(hours) {{
            try {{
                const res = await fetch("/api/owner-test", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRF-Token": csrfToken
                    }},
                    body: JSON.stringify({{
                        action: "EXTEND",
                        additional_hours: hours
                    }})
                }});
                const data = await res.json();
                if (res.ok && data.status === "success") {{
                    showToast("Extended stay by +" + hours + " hours!");
                    const updated = await (await fetch("/api/status")).json();
                    updateStatusUI(updated);
                }} else {{
                    showToast(data.error || "Failed to extend session", true);
                }}
            }} catch (err) {{
                showToast("Network error extending session", true);
            }}
        }}

        async function cancelOwnerTest() {{
            try {{
                const res = await fetch("/api/owner-test", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRF-Token": csrfToken
                    }},
                    body: JSON.stringify({{ action: "CANCEL" }})
                }});
                if (res.ok) {{
                    showToast("Owner Presence Cleared (Returned to Auto)");
                    const updated = await (await fetch("/api/status")).json();
                    updateStatusUI(updated);
                }} else {{
                    showToast("Failed to clear owner session", true);
                }}
            }} catch (err) {{
                showToast("Network error", true);
            }}
        }}

        function toggleMeterEntry() {{
            const form = document.getElementById("mon-meter-entry-form");
            if (form) {{
                form.style.display = (form.style.display === "none" || !form.style.display) ? "block" : "none";
            }}
        }}

        async function submitMeterReading() {{
            const total = parseFloat(document.getElementById("meter-input-total").value);
            if (isNaN(total) || total < 0) {{
                showToast("Please enter a valid meter reading in kWh", true);
                return;
            }}
            const peakVal = document.getElementById("meter-input-peak").value;
            const offpeakVal = document.getElementById("meter-input-offpeak").value;
            const recBy = document.getElementById("meter-input-recorded-by").value || "Owner";
            const notes = document.getElementById("meter-input-notes").value || "";

            const todayStr = new Date().toISOString().split("T")[0];
            const payload = {{
                reading_date: todayStr,
                meter_kwh: total,
                recorded_by: recBy,
                notes: notes
            }};

            try {{
                const res = await fetch("/api/monitoring/meter-reading", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRF-Token": csrfToken
                    }},
                    body: JSON.stringify(payload)
                }});
                const data = await res.json();
                if (res.ok && data.status === "success") {{
                    showToast("Utility meter reading recorded successfully!");
                    document.getElementById("meter-input-total").value = "";
                    document.getElementById("meter-input-peak").value = "";
                    document.getElementById("meter-input-offpeak").value = "";
                    document.getElementById("meter-input-notes").value = "";
                    toggleMeterEntry();
                    const updated = await (await fetch("/api/status")).json();
                    updateStatusUI(updated);
                }} else {{
                    showToast(data.error || "Failed to record meter reading", true);
                }}
            }} catch (err) {{
                showToast("Network error saving meter reading", true);
            }}
        }}

        function viewObservationReport() {{
            window.open("/api/monitoring/report", "_blank");
        }}

        // Initialize UI
        populateForm(initialConfig);
        initialFormSnapshot = getFormValues();
        updateStatusUI(initialStatus);

        // Auto refresh telemetry every 30 seconds
        setInterval(async () => {{
            try {{
                const resp = await fetch("/api/status");
                if (resp.ok) {{
                    const st = await resp.json();
                    updateStatusUI(st);
                }}
            }} catch (e) {{}}
        }}, 30000);
    </script>
</body>
</html>
"""
