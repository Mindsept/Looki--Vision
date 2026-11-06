#!/usr/bin/env python3
"""Opt-in real WebRTC smoke test. Uses the running backend and can incur OpenAI charges.

Run with uv run --with playwright python scripts/test_lio_browser.py ...
The operator key is read from the private .env; only session tokens enter the browser.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402

AUDIO_STATS = """async () => {
  const rows = [];
  for (const pc of window.testPeers) for (const stat of (await pc.getStats()).values()) {
    if (stat.type === 'inbound-rtp' && stat.kind === 'audio') rows.push({
      packetsReceived: stat.packetsReceived, bytesReceived: stat.bytesReceived,
      totalSamplesReceived: stat.totalSamplesReceived, totalAudioEnergy: stat.totalAudioEnergy,
      packetsLost: stat.packetsLost
    });
  }
  return rows;
}"""


async def main():
    from playwright.async_api import async_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--chrome", help="Existing Chrome executable; otherwise install Chromium with Playwright."
    )
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--question", default="Décris ce qu’il y a devant moi.")
    parser.add_argument(
        "--interrupt-after-audio",
        action="store_true",
        help="Stop on first received audio and verify a new media epoch.",
    )
    parser.add_argument(
        "--alert-image",
        type=Path,
        help="Upload a second real image during audio; expect a real proximity alert and new epoch.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output/lio-browser"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=15,
        headers={"Authorization": f"Bearer {Settings().api_key}"},
    ) as api:
        session = await api.post("/api/v1/sessions", json={"center_id": "demo-centre"})
        session.raise_for_status()
        sid = session.json()["id"]
        try:
            response = await api.post(f"/api/v1/sessions/{sid}/access-tokens")
            response.raise_for_status()
            tokens = response.json()
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    executable_path=args.chrome,
                    args=["--autoplay-policy=no-user-gesture-required"],
                )
                try:
                    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
                    # Observe transport statistics without injecting provider commands.
                    await page.add_init_script("""const NativePeer = window.RTCPeerConnection;
                        window.testPeers = [];
                        window.RTCPeerConnection = class extends NativePeer {
                          constructor(...args) { super(...args); window.testPeers.push(this); }
                        };""")
                    errors = []
                    page.on("pageerror", lambda error: errors.append(type(error).__name__))
                    await page.goto(args.base_url + "/live/?assistant=1")
                    await page.get_by_label("Session LOOKi", exact=True).fill(sid)
                    await page.get_by_label("Jeton mobile", exact=True).fill(tokens["mobile_token"])
                    await page.get_by_role("button", name="Préparer l’audio", exact=True).click()
                    await (
                        page.get_by_role("status")
                        .filter(has_text="Audio : connected")
                        .wait_for(timeout=45000)
                    )
                    await page.get_by_text(
                        "Rejouer un fichier JPEG réel pour cet essai", exact=True
                    ).click()
                    await page.get_by_label("Jeton Pi", exact=True).fill(tokens["pi_token"])
                    await page.locator('input[type="file"]').set_input_files(args.image)
                    await page.get_by_label("Question finale", exact=True).fill(args.question)
                    async with page.expect_response(
                        lambda response: (
                            response.request.method == "POST"
                            and response.url.endswith(f"/api/v1/sessions/{sid}/questions")
                        )
                    ) as admitted:
                        await page.get_by_role(
                            "button", name="Poser la question", exact=True
                        ).click()
                    question = await (await admitted.value).json()
                    interrupt = args.interrupt_after_audio or args.alert_image is not None
                    if interrupt:
                        # An idle WebRTC connection can already receive comfort-noise packets.
                        # Wait for THIS response's buffer-start event before examining energy.
                        await page.wait_for_function(
                            """() => /Début d’émission observé côté navigateur : [0-9]+ ms/.test(document.body.innerText)""",
                            timeout=45000,
                        )
                        # Save the qualifying sample itself; a later RTC report can reset
                        # counters when an SSRC changes, and idle packets are not speech.
                        for _ in range(100):
                            before_interrupt = await page.evaluate(AUDIO_STATS)
                            if any(
                                stat.get("totalAudioEnergy", 0) > 0
                                and stat.get("bytesReceived", 0) > 1024
                                for stat in before_interrupt
                            ):
                                break
                            await asyncio.sleep(0.1)
                        else:
                            raise AssertionError("No audible-energy RTP sample before interruption")
                        old = (
                            await api.get(f"/api/v1/sessions/{sid}/realtime/calls/current")
                        ).json()
                        if args.alert_image:
                            for _ in range(3):
                                uploaded = await api.post(
                                    f"/api/v1/sessions/{sid}/frames",
                                    headers={"Authorization": f"Bearer {tokens['pi_token']}"},
                                    data={
                                        "frame_id": str(uuid4()),
                                        "timestamp": datetime.now(UTC).isoformat(),
                                        "latest_only": "true",
                                    },
                                    files={
                                        "image": (
                                            "alert.jpg",
                                            args.alert_image.read_bytes(),
                                            "image/jpeg",
                                        )
                                    },
                                )
                                uploaded.raise_for_status()
                                await asyncio.sleep(0.25)
                            await page.get_by_role(
                                "heading", name="Alerte backend", exact=True
                            ).wait_for(timeout=10000)
                        else:
                            await page.get_by_role(
                                "button", name="Arrêter / fermer l’appel", exact=True
                            ).click()
                        await (
                            page.get_by_role("status")
                            .filter(has_text="Contrôle : closed")
                            .wait_for(timeout=35000)
                        )
                        locally_muted = await page.locator("audio").evaluate(
                            "audio => audio.muted && audio.srcObject === null"
                        )
                        assert locally_muted
                        await page.get_by_role(
                            "button", name="Préparer l’audio", exact=True
                        ).click()
                        await (
                            page.get_by_role("status")
                            .filter(has_text="Audio : connected")
                            .wait_for(timeout=45000)
                        )
                        new = (
                            await api.get(f"/api/v1/sessions/{sid}/realtime/calls/current")
                        ).json()
                        assert new["media_epoch"] > old["media_epoch"]
                    # Wait for a durable terminal generation, not an arbitrary rendering delay.
                    await page.wait_for_function(
                        """() => /Génération : (completed|failed|cancelled|unavailable)/.test(document.body.innerText)""",
                        timeout=45000,
                    )
                    terminal = await page.locator("blockquote").inner_text()
                    stats = before_interrupt if interrupt else await page.evaluate(AUDIO_STATS)
                    diagnostics = json.loads(await page.locator("pre").inner_text())
                    final_question = (
                        await api.get(f"/api/v1/sessions/{sid}/questions/{question['request_id']}")
                    ).json()
                    result = {
                        "session_id": sid,
                        "generation": diagnostics,
                        "question_status": final_question["status"],
                        "media_status": final_question["media_status"],
                        "inbound_audio": stats,
                        "transcript": terminal,
                        "page_errors": errors,
                        "interruption_and_new_epoch": interrupt,
                        "local_mute_confirmed": locally_muted if interrupt else None,
                        "interrupt_reason": "alert"
                        if args.alert_image
                        else "user_stop"
                        if interrupt
                        else None,
                        "media_epochs": [old["media_epoch"], new["media_epoch"]]
                        if interrupt
                        else None,
                        "listened_on_iphone": False,
                        "headless_autoplay_override": True,
                    }
                    (args.output_dir / "result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                    )
                    # Hide credentials before capturing the page.
                    await page.evaluate(
                        "document.querySelectorAll('input[type=password]').forEach(el => el.value = '')"
                    )
                    await page.screenshot(path=str(args.output_dir / "screen.png"), full_page=True)
                    await page.get_by_role(
                        "button", name="Arrêter / fermer l’appel", exact=True
                    ).click()
                    await (
                        page.get_by_role("status")
                        .filter(has_text="Contrôle : closed")
                        .wait_for(timeout=35000)
                    )
                    result["remote_close_confirmed"] = True
                    (args.output_dir / "result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                    )
                    print(
                        json.dumps(
                            {
                                "result": str(args.output_dir / "result.json"),
                                "provider_status": diagnostics.get("provider_status"),
                                "audio_received": any(s.get("bytesReceived", 0) > 0 for s in stats),
                                "page_errors": errors,
                            }
                        )
                    )
                except Exception:
                    # Capture only a redacted screen and public status, never inputs/SDP.
                    await page.evaluate(
                        "document.querySelectorAll('input[type=password]').forEach(el => el.value = '')"
                    )
                    await page.screenshot(path=str(args.output_dir / "failure.png"), full_page=True)
                    status = await page.locator('[role="status"]').inner_text()
                    errors_on_screen = await page.locator('[role="alert"]').all_inner_texts()
                    print(
                        json.dumps(
                            {"status": status, "errors": errors_on_screen}, ensure_ascii=False
                        )
                    )
                    raise
                finally:
                    await browser.close()
        finally:
            await api.post(f"/api/v1/sessions/{sid}/close")


if __name__ == "__main__":
    asyncio.run(main())
