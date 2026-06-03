"""
Compare API response time: before vs after Celery.

Before Celery: import endpoint blocked 60-180s waiting for STT pipeline.
After Celery:  import endpoint returns immediately (<2s), worker processes in background.

Usage:
    python scripts/test_celery_speed.py
"""

import time

import httpx

BASE_URL = "http://localhost:8000/api/v1"
TEST_VIDEO_URL = "https://www.youtube.com/watch?v=F98NLtswkA8"


def get_token() -> str:
    """Login and get access token."""
    resp = httpx.post(f"{BASE_URL}/auth/login", json={
        "email": "admin@gmail.com",
        "password": "admin123",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def delete_video_if_exists(token: str, youtube_id: str):
    """Delete video if it already exists (to allow re-import)."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(f"{BASE_URL}/videos", headers=headers)
    for video in resp.json():
        if video["youtube_id"] == youtube_id:
            httpx.delete(f"{BASE_URL}/videos/{video['id']}", headers=headers)
            print(f"  Deleted existing video: {video['id']}")
            return


def test_import_speed(token: str):
    """Measure how fast the import endpoint responds."""
    headers = {"Authorization": f"Bearer {token}"}

    print("\n" + "=" * 60)
    print("TEST: Import endpoint response time (with Celery)")
    print("=" * 60)

    # Clean up first
    delete_video_if_exists(token, "F98NLtswkA8")
    time.sleep(1)

    # Measure import response time
    print(f"\n  Importing: {TEST_VIDEO_URL}")
    t0 = time.time()

    resp = httpx.post(
        f"{BASE_URL}/videos/import",
        json={"youtube_url": TEST_VIDEO_URL},
        headers=headers,
        timeout=300,
    )

    elapsed = time.time() - t0
    status = resp.status_code

    print(f"\n  Response status: {status}")
    print(f"  Response time:   {elapsed:.2f}s")

    if status in (200, 201):
        data = resp.json()
        print(f"  Video ID:        {data.get('id')}")
        print(f"  Status:          {data.get('transcription_status')}")
        print(f"  Auto-generated:  {data.get('is_auto_generated')}")

        # If status is pending, poll until ready
        if data.get("transcription_status") == "pending":
            print(f"\n  STT dispatched to Celery worker. Polling for completion...")
            video_id = data["id"]
            poll_start = time.time()

            while True:
                time.sleep(3)
                poll_resp = httpx.get(
                    f"{BASE_URL}/videos/{video_id}/transcription-status",
                    headers=headers,
                )
                poll_data = poll_resp.json()
                status = poll_data["status"]
                poll_elapsed = time.time() - poll_start

                print(f"    [{poll_elapsed:5.1f}s] status = {status}")

                if status in ("ready", "failed"):
                    break
                if poll_elapsed > 300:
                    print("    Timeout waiting for STT")
                    break

            total_time = time.time() - t0
            print(f"\n  Total pipeline time: {total_time:.2f}s")
            print(f"  User waited:         {elapsed:.2f}s (API response)")
            print(f"  Background work:     {total_time - elapsed:.2f}s (Celery worker)")

            if status == "failed":
                print(f"  Error: {poll_data.get('error')}")

    elif status == 206:
        print(f"  Partial: {resp.json().get('message')}")
    else:
        print(f"  Error: {resp.text[:200]}")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Before Celery: ~60-180s (user blocked waiting)")
    print(f"  After Celery:  {elapsed:.2f}s (user sees video immediately)")
    print(f"  Speedup:       ~{int(90 / max(elapsed, 0.1))}x faster response")
    print("=" * 60)


if __name__ == "__main__":
    print("Logging in...")
    token = get_token()
    print("OK")
    test_import_speed(token)