import time
import requests
import numpy as np

RENDER_URL = "https://sulit-fastapi.onrender.com/api/v1/events/transient"
DEVICE_ID = "TEST_DEVICE_01"

# Synthesize 30 kHz transient signal (6,000 samples)
t = np.linspace(0, 0.2, 6000, endpoint=False)
signal = 2.5 * np.sin(2 * np.pi * 60 * t) + 0.8 * np.sin(2 * np.pi * 180 * t) + np.random.normal(0, 0.05, len(t))

# 1. Send TURN_ON Event (+800 W Air Conditioner)
turn_on_payload = {
    "device_id": DEVICE_ID,
    "p_before": 50.0,
    "p_after": 850.0,
    "pf_before": 0.85,
    "pf_after": 0.97,
    "raw_samples": signal.tolist()
}

print("1. Transmitting TURN_ON event (+800 W Air Conditioner)...")
res1 = requests.post(RENDER_URL, json=turn_on_payload, timeout=20)
print("Status Code:", res1.status_code)

try:
    print("Response:", res1.json())
except Exception:
    print("Raw Response Text:", res1.text)

if res1.ok:
    print("\nLook at your browser dashboard now!")
    # Wait 8 seconds to view live update
    time.sleep(8)

    # 2. Send TURN_OFF Event (-800 W Air Conditioner)
    turn_off_payload = {
        "device_id": DEVICE_ID,
        "p_before": 850.0,
        "p_after": 50.0,
        "pf_before": 0.97,
        "pf_after": 0.85,
        "raw_samples": signal.tolist()
    }

    print("\n2. Transmitting TURN_OFF event (-800 W Air Conditioner)...")
    res2 = requests.post(RENDER_URL, json=turn_off_payload, timeout=20)
    print("Status Code:", res2.status_code)
    try:
        print("Response:", res2.json())
    except Exception:
        print("Raw Response Text:", res2.text)