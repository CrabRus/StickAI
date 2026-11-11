"""User-tweakable settings."""

# Stickman height in device-independent pixels (scaled by the primary monitor's DPI).
STICKMAN_HEIGHT_DIP = 84

# The four default characters' colours (flat fills, no shading).
COLORS = {
    "yellow": "#FEC503",
    "blue": "#33CCFF",
    "red": "#D91100",
    "green": "#66CC00",
}

# How often to re-read window positions from the OS. The window a stickman
# stands on is additionally re-read every frame so he rides it smoothly.
SCENE_POLL_MS = 50
