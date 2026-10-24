import base64
from datetime import datetime, timezone
from importlib.resources import files


def console_styles():
    assets = files("abfe") / "console_assets"
    faces = []
    for family, weight, filename in (
        ("Inter", 400, "Inter-Regular.ttf"),
        ("Inter", 600, "Inter-SemiBold.ttf"),
        ("JetBrains Mono", 600, "JetBrainsMono-SemiBold.ttf"),
    ):
        encoded = base64.b64encode((assets / filename).read_bytes()).decode("ascii")
        faces.append(
            f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;font-display:swap;src:url(data:font/ttf;base64,{encoded}) format('truetype');}}"
        )
    return "\n".join(faces) + "\n" + (assets / "console.css").read_text()


def console_script():
    return (files("abfe") / "console_assets/console.js").read_text()


def display_time(value):
    if not value:
        return "Not checked"
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .strftime("%d %b %Y · %H:%M UTC")
        )
    except (ValueError, TypeError):
        return "Unknown"
