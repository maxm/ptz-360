"""Constants for PTZ 360."""

DOMAIN = "ptz_360"
STORAGE_VERSION = 1

# Options: where the picture comes from (config_flow.py).
CONF_VIDEO_ENTITY = "video_entity"
CONF_STREAM_URL = "stream_url"

# The calibration pass (calibrate.py).
# Zooms where a nudge measures how many pan / tilt units a frame spans (a first guess for the model fit).
NUDGE_ZOOMS = (0.0, 0.25, 0.5, 0.75, 1.0)
# How far a nudge moves, in frames (a phase correlation finds shifts under half a frame).
NUDGE_FRAMES = 0.2
# Zooms the camera model is fitted at: close together at the wide end, where the lens's bending fades out.
MODEL_ZOOMS = (0.0, 0.07, 0.1, 0.14, 0.25, 0.5, 0.75, 1.0)

# The frontend card, served by the integration and added to every dashboard.
CARD_URL = "/ptz_360/ptz-360-card.js"
