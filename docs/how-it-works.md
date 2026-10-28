# How PTZ 360 works

PTZ 360 turns a pan / tilt / zoom camera into a "360° video". You drag or pinch a virtual camera over a sphere. The view moves at once, the real camera follows by absolute position and arrives a moment later, and what it can't see live is filled in from a panorama of everything the camera can see.

Two problems have to be solved:

- **Where a frame is.** Video frames carry no position. The camera's position reports are late and don't line up exactly with its picture. So every live frame is placed from the picture itself.
- **What to show elsewhere.** A panorama stitched once goes stale (cars, people, light), so it's repainted from what the camera sees.

This approach was first built in a native app and then measured frame by frame with a record-and-replay tool on one camera (a Dahua SD49225DB-HNY). The numbers below come from there.

## 1. Calibration: the camera model and the panorama

The integration does this once, in one pass (Calibrate in the card, or the Calibrate button). The camera moves around and comes back.

1. **Nudges.** At five zooms the camera moves a fifth of a frame and back. Phase correlation between the frames tells how many pan / tilt units a frame spans, which is the first guess for the fit.
2. **Model frames and the fit.** Overlapping frames at several zooms are matched patch by patch, and a camera model is fitted to them (Levenberg–Marquardt):
   - yaw = `ap`·pan; pitch = `at`·tilt + `bt` (degrees);
   - the focal length `f(zoom)`, in image widths, log-linear between the measured zooms;
   - the lens's radial distortion r(1 + k1 r² + k2 r⁴), with k1 / k2 per zoom;
   - a pan that covers a full turn wraps (`ap` = 360 / range).
   The fit's rms error (in pixels) is shown when it finishes. Well under 1 px is good.
3. **Sweep.** The camera visits its whole range at the widest zoom, frames 0.6 of a frame apart.
4. **Stitch.** The frames become one equirectangular picture (x = yaw, y = pitch, 12 px per degree) in the model's world. Each frame is placed by the model from its reported position, corrected by matching it against its neighbours (but kept within 0.3° of the report), then brightness is evened out and the frames are feathered together.

The model and the panorama are stored by the integration and shared by every browser.

## 2. Following a move

All of this runs in the card, in your browser.

- **The view** is a virtual camera with the real camera's lens. Drag turns it, pinch or the wheel zooms. Only the latest target is sent to the camera, one move at a time, and the camera's position is polled every 150 ms until it stops.
- **Drawing** is one WebGL shader per pixel: undistort for the view's lens, look up the panorama, and where the live frame covers that pixel, show the live frame through the camera model. Seen from where it was taken, a frame lands exactly as recorded. The panorama is drawn desaturated, so the live picture stands out.
- **Each new video frame** (grey, 160×90) is placed in a background worker:
  - **Against the previous still frame (the anchor), every frame.** The frame is straightened through the lens model and found in the anchor by normalised correlation, coarse to fine (shift, and scale while zooming), leaving out the camera's on-screen clock. Where its centre lands gives its pose. When it has moved far from the anchor (scale over 1.4 or under 0.7, or more than a quarter of the frame) it becomes the new anchor.
  - **Against the panorama, every 4th frame** (and before every new anchor). The frame is found in the panorama rendered at the believed pose. A match is accepted at a correlation of 0.55 or more, within 10% scale and 15% shift, and applied part of the way (30%, or 15% while zooming). This keeps small errors from adding up.
- **After the video stalls,** the frame is found again in the panorama instead (see "What we learned" for why).

## 3. When the camera stops: settle and the report offset

When the position stops changing and the picture has been still for 3 frames, the live video takes over at once, where the last frame was placed.

Beside it, without holding anything up, the card takes a frame from after the camera stopped and finds it in the panorama once more, at full weight. That answer is used three ways:

- **It teaches the report offset.** Cameras don't report exactly what their picture shows. The test camera's picture was consistently 0.6–1.0° higher than its reported tilt, and ±0.2° off in pan depending on which way it last turned (gear play). The card learns the picture's position minus the reported one, separately for turning left and right. It subtracts it from where it sends the camera and adds it to what the camera reports. Without this, the view glides up a little at the end of every pan, and the next pan starts from there, so the view creeps.
- **It confirms the spot** for the repaint (below).
- **If the live view is off,** it moves the view and the live picture together, so the picture stays put on screen and only the panorama around it shifts slightly.

Only confident, small and consistent readings teach the offset. A reading counts only when:
- its correlation is 0.7 or more (well past the 0.55 a match needs);
- it's under 3°;
- once there are three readings, it's within 1.5° of their median.

The offset is the median of the last seven readings. One wrong match can't move it.

The offset is learnt and kept per browser (in local storage), because it partly comes from how each client decodes frames: on the same camera, two different apps learnt pitch offsets 0.2–0.4° apart.

## 4. Repainting the panorama

The panorama is repainted locally from what the camera actually sees, so a car that has left disappears once the camera has seen the spot empty. Only **confirmed** pictures are painted: the still frame after the end-of-move check, and the live picture while the camera rests at a confirmed place (every 1.5 s). The frame's edges and the bottom 12% (where cameras usually draw their clock) are left out, and it's feathered in.

The repaint is kept per browser (in IndexedDB), and only for drawing. Matching always uses the original stitched panorama.

## 5. What we learned

These come from recording every frame of real sessions and replaying them through the same code. Replay is the only way to compare changes, because live sessions can't be repeated: the video freezes at random.

1. **The video freezes.** No new frame for 0.3–2 s, several times a minute while the camera turns (one 7-move session had 41 gaps, 34 s in total), and sometimes while it's still. The freezes don't follow the moves sent to the camera, so spacing those out doesn't help. **Every large jump we saw traced back to a freeze:** the camera keeps turning, so the first frame afterwards is 10–25° away from the last.
2. **Guessing from the reports after a freeze is wrong.** Placing the frame where the camera reported it some time ago was up to 13° off, and every later frame of the move was placed relative to it. Instead, the first frame after a gap of over 0.3 s (or after four misses in a row) is **found again in the panorama**, around the camera's latest report.
3. **A weak match right after a freeze is usually wrong.** One frame matched at 0.28 landed 22° off. A match under 0.5 right after a gap counts as a miss.
4. **A freeze as the camera stops can strand the view.** Giving up after 2 s without frames left the view on a frame from mid-pan, once 28.7° off. The card now waits up to 8 s.
5. **Anything slow at the end of a move shows.** Holding the live view back for the panorama check looked like the video freezing at the end of every pan, and gliding to the answer seconds later looked like a second move. So the live view takes over at once and the check runs beside it.
6. **Repainting unconfirmed frames makes mistakes permanent.** When tracking was slow, frames were painted where tracking wrongly thought they were. The repaint ended up with three copies of a house. Only confirmed pictures are painted now.
7. **One wrong match can poison the offset.** A few correlation matches at the end of a move were wrong (one +13°), and an offset learnt from every reading took them in. That's why only confident, small and consistent readings count.
8. **Slow devices suffer most.** On a phone the matching runs far slower. A per-move timing log found the problems in one session. Test on the slowest device you care about.

With these fixes, replays of the same recorded sessions went from moves ending 9–11° off to all of them within about 1°. With the offset learnt, the end-of-pan glide dropped from ~0.9° to about 0.1° or less.

## 6. Not there yet

- **A learned matcher.** The native app also uses a learned feature matcher (DISK + LightGlue, Apache-2.0 weights). It re-finds frames after long freezes that correlation can't, especially with trees in changing light. In replays without it, most moves end fine, but freeze-heavy ones can end several degrees off. The open question for the card is its speed in a phone browser: natively on a phone it already takes 1–3 s per match.
- **A smooth catch-up after long freezes.** After a long freeze the view now lands in the right place, but it cuts there instead of gliding.
- **A shared repaint.** The repaint is per browser. It could go back to the integration so every browser benefits, as long as only confirmed pictures are sent.
- **The offset across zoom and range.** It was only measured at the widest zoom, near the horizon. It may differ when zoomed in or looking far up or down.
- **More cameras.** Everything was tuned on one camera. Reports from other models are the most useful thing right now.

## What a camera needs

- ONVIF absolute moves, and a status that reports its position. Some cameras keep saying "moving" once still, so an unchanged position counts as stopped.
- Positions it returns to repeatably, to well under a frame. Backlash of a few tenths of a degree is fine, because it's learnt per direction.
- A video stream: WebRTC through Home Assistant, or MJPEG as the fallback. Freezes are expected.

## Constants

| | |
|---|---|
| Grey frame | 160×90 |
| Anchor match | coarse ≥ 0.4; a new anchor at scale > 1.4 or < 0.7, or a shift > 25% of the frame |
| Panorama match | every 4th frame; score ≥ 0.55, scale within 10%, shift within 15%; gain 0.3 (0.15 zooming) |
| After a freeze | gap > 0.3 s; weak match < 0.5; found again after the first miss after a gap, or 4 in a row; give up after 8 s without frames |
| Settle | 3 still frames; the check uses a frame from after the stop (waits up to 2 s) |
| Report offset | readings with score ≥ 0.7 and under 3°; median of the last 7; once 3, set aside one more than 1.5° from the median |
| Panorama | 12 px per degree; sweep frames 0.6 of a frame apart; stitch kept within 0.3° of the reports |
