# PTZ 360

An ONVIF pan / tilt / zoom camera as a 360° view in your Home Assistant dashboard. Drag the picture to look around and pinch (or scroll) to zoom. The view moves at once over a panorama of everything the camera can see, and the camera follows. When it gets there, the live picture lands exactly where the view is.

It's self-contained. You need the camera's ONVIF login, nothing else: no Frigate, no other camera integration.

> Status: early proof of concept. Tested with one camera (a Dahua SD49225DB-HNY).

https://github.com/user-attachments/assets/5eab2ef0-d4ce-439b-9e58-cf69aaf8f102

## What it does

- **Camera entity.** A camera entity streams the camera's RTSP feed, so Home Assistant's built-in WebRTC (go2rtc) plays it.
- **One calibration pass** (a few minutes; the camera moves around and comes back):
  1. Measures how far a frame spans at a few zooms.
  2. Fits the camera's geometry: degrees per pan / tilt unit, the focal length at each zoom, and the lens's distortion.
  3. Sweeps the whole range at the widest zoom and stitches it into one panorama.
- **The `ptz-360-card` card:** draws the panorama and the live picture through the camera's lens model (WebGL). It finds every frame of a move in the one before (normalised correlation, in a Web Worker), checks it against the panorama, and finds frames again after the video stalls (it does). The panorama is repainted from what the camera has seen where it stopped, kept in your browser.

## Install

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/maxm/ptz-360` (Integration), then install **PTZ 360** and restart Home Assistant.
2. Settings → Devices & services → Add integration → **PTZ 360**. Enter the camera's address, ONVIF port and login.
3. Add the card to a dashboard:

   ```yaml
   type: custom:ptz-360-card
   entity: camera.your_camera
   ```

4. The card offers to **Calibrate** (admins only). Let it run; the camera comes back to where it was. To run it again later, use the ⋯ menu in the card, or the camera's **Calibrate** button (Settings → Devices). The **Calibration** sensor shows how it's going.
5. Optionally, ⋯ → **Set this view as home**: the home button goes there. Until you set one, it goes to the camera's own ONVIF home position.

### Video

By default the picture comes from the stream the camera names over ONVIF, played with Home Assistant's WebRTC (go2rtc). If WebRTC can't reach the camera, the card falls back to MJPEG. If another integration already shows this camera (Frigate, say), you can use its picture instead: Settings → Devices & services → PTZ 360 → **Configure** → *Video from another camera entity*. In the same dialog you can set a different RTSP address for the integration's own camera entity.

## Testing

PTZ 360 has only been tried on one camera so far, so reports from any other camera help, including "it just works". Please open a [camera report](https://github.com/maxm/ptz-360/issues/new?template=camera-report.yml) with:

- the camera's make and model, and your Home Assistant version;
- whether calibration finished, and its fit (the **Calibration** sensor's `fit_rms` attribute, in pixels; well under 1 is good);
- where you used the card (desktop, the Home Assistant app on iPhone or Android, a phone browser) and which video it played (the card's ⋯ menu says WebRTC or MJPEG);
- how well the live picture lands where the view is after a pan or a zoom. A short screen recording helps a lot.

Before you start:
- **Calibration moves the camera around its whole range** for about 15 minutes (it comes back where it was). Don't run it while you need the camera, and don't move the camera during it.
- The learnt correction and the repainted panorama are kept per browser, so the first few pans on a new device can land a little off before it has learnt the correction.

## Troubleshooting

- **"Custom element doesn't exist: ptz-360-card" (Configuration error) right after Home Assistant restarts:** the page loaded before the integration had added the card. Reload the page.

## Requirements

- An ONVIF camera with absolute pan / tilt / zoom moves and position reporting.
- Home Assistant 2025.1 or newer.
- A browser with WebGL 2 (all current ones).

## How it works

[docs/how-it-works.md](docs/how-it-works.md) describes the approach, what was measured, and why the card does what it does (video stalls, the offset between the camera's reports and its picture, repainting only confirmed places).

## Developing

`dev/docker-compose.yml` runs a throwaway Home Assistant (on Docker Desktop for Mac, WebRTC can't reach the container, so the card uses the MJPEG fallback there) with the integration mounted. `dev/ha.py` and `dev/ws.py` call its REST and websocket APIs with a local test login (`dev/local-login.json`, not committed).
