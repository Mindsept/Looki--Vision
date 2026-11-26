#!/usr/bin/env python3
"""Opt-in real GCP vision test, using the embedded sender and an isolated Chrome profile.

No OpenAI calls. Requires a real YOLO backend and private Pi/viewer credential files.
uv run --no-sync --with playwright --with opencv-python-headless python scripts/test_pi_browser.py --help
"""

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import httpx


async def main():
    from playwright.async_api import async_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer", type=Path, required=True)
    parser.add_argument("--pi-env", type=Path, required=True)
    parser.add_argument("--sender", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--chrome")
    parser.add_argument("--output-dir", type=Path, default=Path("output/pi-gcp-browser"))
    args = parser.parse_args()
    credentials = json.loads(args.viewer.read_text())
    pi = {}
    for line in args.pi_env.read_text().splitlines():
        parts = shlex.split(line)
        if len(parts) == 2 and parts[0] == "export":
            name, value = parts[1].split("=", 1)
            pi[name] = value
    base, sid = credentials["backend_url"], credentials["session_id"]
    assert pi["LOOKI_BASE_URL"] == base and pi["LOOKI_SESSION_ID"] == sid
    args.output_dir.mkdir(parents=True, exist_ok=True)
    received, errors, browser_writes = {}, [], []
    process = None
    async with httpx.AsyncClient(
        base_url=base,
        timeout=30,
        headers={
            "Authorization": "Bearer " + credentials["mobile_token"],
        },
    ) as api:
        initial = (await api.get(f"/api/v1/sessions/{sid}/live")).raise_for_status().json()
        assert initial["runtime"]["vision_provider"] == "yolo_seg_track"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, executable_path=args.chrome)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                await page.add_init_script("""const NativeSocket = window.WebSocket;
                    window.testSockets = [];
                    window.WebSocket = class extends NativeSocket {
                      constructor(...args) { super(...args); window.testSockets.push(this); }
                    };""")
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "request",
                    lambda req: (
                        browser_writes.append(req.url.split("?")[0])
                        if req.method == "POST"
                        else None
                    ),
                )

                def socket_opened(socket):
                    def frame(data):
                        event = json.loads(data)
                        if event.get("id") is not None:
                            received[event["id"]] = event

                    socket.on("framereceived", frame)

                page.on("websocket", socket_opened)

                async def attach():
                    await page.get_by_label(
                        "Importer la configuration Pi", exact=True
                    ).set_input_files(args.viewer)
                    await page.get_by_role(
                        "button", name="Se connecter aux lunettes", exact=True
                    ).click()
                    await page.get_by_text("WebSocket connecté", exact=True).wait_for(timeout=30000)

                await page.goto(base + "/live/?mode=pi", wait_until="load")
                await attach()
                print(
                    "Viewer connected with a session token; sending the real recording at 2 captures/s.",
                    flush=True,
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(args.sender),
                    "--source",
                    "video",
                    "--video-file",
                    str(args.video),
                    "--base-url",
                    base,
                    "--session-id",
                    sid,
                    "--fps",
                    "2",
                    "--max-frames",
                    "60",
                    env={**os.environ, "LOOKI_PI_TOKEN": pi["LOOKI_PI_TOKEN"]},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                image = page.get_by_alt_text(
                    "Capture exacte associée au résultat affiché", exact=True
                )
                await image.wait_for(timeout=60000)
                await page.get_by_role("button", name="Figer l’affichage", exact=True).click()
                await asyncio.sleep(0.5)
                frozen = await image.get_attribute("src")
                await asyncio.sleep(2)
                assert await image.get_attribute("src") == frozen
                await page.get_by_role("button", name="Reprendre le direct", exact=True).click()
                # Close a real WSS connection; do not inject frames or provider events.
                socket_count = await page.evaluate("window.testSockets.length")
                await page.evaluate("window.testSockets.at(-1).close()")
                await page.wait_for_function(
                    "count => window.testSockets.length > count && window.testSockets.at(-1).readyState === WebSocket.OPEN",
                    arg=socket_count,
                    timeout=15000,
                )
                output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
                (args.output_dir / "sender.log").write_bytes(output)
                assert process.returncode == 0, "Embedded sender failed; see sender.log"
                deadline = asyncio.get_running_loop().time() + 60
                while not any(e["type"] == "frame.depth_analyzed" for e in received.values()):
                    assert asyncio.get_running_loop().time() < deadline, (
                        "No real depth result received"
                    )
                    await asyncio.sleep(1)
                await page.screenshot(path=str(args.output_dir / "desktop.png"), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 900})
                await page.screenshot(path=str(args.output_dir / "mobile.png"), full_page=True)
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    "Horizontal overflow"
                )
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.get_by_role("button", name="Voir cette capture", exact=True).click()
                await page.get_by_text("PROFONDEUR CONSULTÉE", exact=True).wait_for(timeout=15000)
                await page.screenshot(path=str(args.output_dir / "depth.png"), full_page=True)
                if await page.locator(".alert-row").count():
                    await page.locator(".alert-row").first.click()
                    await page.get_by_text("ALERTE CONSULTÉE", exact=True).wait_for(timeout=15000)
                await page.get_by_role("button", name="Déconnecter le viewer", exact=True).click()
                session = (await api.get(f"/api/v1/sessions/{sid}")).raise_for_status().json()
                assert session["status"] == "active", "Viewer closed the Pi session"
                await page.get_by_role(
                    "button", name="Se connecter aux lunettes", exact=True
                ).click()
                await image.wait_for(timeout=15000)
                await page.reload()
                assert await page.get_by_label("Jeton du viewer", exact=True).input_value() == ""
                await attach()
                await image.wait_for(timeout=15000)
                assert not errors, errors
                assert all(url.endswith("/ws-ticket") for url in browser_writes), browser_writes
                analyzed = [e for e in received.values() if e["type"] == "frame.analyzed"]
                assert analyzed and all(e["payload"]["simulated"] is False for e in analyzed)
                summary = {
                    "base_url": base,
                    "session_id": sid,
                    "source": args.video.name,
                    "physical_pi_tested": False,
                    "provider": initial["runtime"],
                    "event_counts": {
                        kind: sum(e["type"] == kind for e in received.values())
                        for kind in sorted({e["type"] for e in received.values()})
                    },
                    "labels": sorted(
                        {d["label"] for e in analyzed for d in e["payload"]["detections"]}
                    ),
                    "pause_keeps_image": True,
                    "wss_reconnected": True,
                    "viewer_disconnect_keeps_session_active": True,
                    "reload_clears_credentials": True,
                    "depth_and_alert_inspection": True,
                    "no_browser_capture_or_close": True,
                    "browser_errors": errors,
                }
                (args.output_dir / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
                (args.output_dir / "events.json").write_text(
                    json.dumps(list(received.values()), indent=2) + "\n"
                )
                print(json.dumps(summary, indent=2), flush=True)
            finally:
                if process and process.returncode is None:
                    process.terminate()
                    await process.communicate()
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
