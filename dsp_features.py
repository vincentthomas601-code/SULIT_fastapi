import numpy as np

APPLIANCE_CLASSES = [
    "Air Conditioner", "Fan", "Fridge", "Hair Dryer", "Heater",
    "Kettle", "Lighting", "Microwave", "Pump", "Toaster", "Vacuum"
]


def _extract_window_metrics(win: np.ndarray) -> np.ndarray:
    if win.size == 0:
        return np.zeros(20, dtype=np.float32)

    rms_val = float(np.sqrt(np.mean(win ** 2)))
    peak_val = float(np.max(np.abs(win)))
    crest_factor = peak_val / (rms_val + 1e-6)
    std_val = float(np.std(win))

    diff_win = np.diff(win)
    max_slope = float(np.max(np.abs(diff_win))) if diff_win.size > 0 else 0.0
    mean_slope = float(np.mean(np.abs(diff_win))) if diff_win.size > 0 else 0.0

    zc_count = float(len(np.where(np.diff(np.signbit(win)))[0]))

    fft_spectrum = np.abs(np.fft.rfft(win))
    # 60 Hz fundamental bin for 3000 samples @ 30 kHz (10 Hz/bin -> Bin 6)
    if fft_spectrum.size > 6:
        fundamental_idx = 6
    elif fft_spectrum.size > 1:
        fundamental_idx = int(np.argmax(fft_spectrum[1:]) + 1)
    else:
        fundamental_idx = 0

    i1 = float(fft_spectrum[fundamental_idx]) + 1e-6 if fft_spectrum.size > 0 else 1e-6

    harmonics = []
    for h in range(2, 13):
        h_idx = min(fundamental_idx * h, max(0, fft_spectrum.size - 1))
        h_val = float(fft_spectrum[h_idx]) / i1 if fft_spectrum.size > 0 else 0.0
        harmonics.append(h_val)

    thd = float(np.sqrt(np.sum(np.array(harmonics, dtype=np.float32) ** 2)))

    return np.array([
        rms_val, peak_val, crest_factor, std_val,
        max_slope, mean_slope, zc_count, thd, i1, *harmonics
    ], dtype=np.float32)


def extract_features(
    raw_samples: list[float],
    p_before: float,
    p_after: float,
    pf_before: float,
    pf_after: float,
    target_dim: int = 40
) -> np.ndarray:
    """
    Extracts a fused DSP + steady-state feature vector matching the ONNX model's
    expected input dimensionality (40D default, 22D legacy fallback).
    """
    signal = np.nan_to_num(np.array(raw_samples, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)

    if signal.size < 4:
        return np.zeros(target_dim, dtype=np.float32)

    # Split into pre-event and post-event windows
    midpoint = signal.size // 2
    pre_window = signal[:midpoint]
    post_window = signal[midpoint:]

    # Remove ADC DC offset per window independently
    pre_window = pre_window - np.mean(pre_window)
    post_window = post_window - np.mean(post_window)

    # Steady-state step changes (PZEM)
    delta_p = float(p_after - p_before)
    delta_pf = float(pf_after - pf_before)

    pre_metrics = _extract_window_metrics(pre_window)
    post_metrics = _extract_window_metrics(post_window)
    diff_metrics = post_metrics - pre_metrics

    # Fuse step features (2) + differential transient metrics (20) + post-step steady metrics (18) = 40D
    features = np.concatenate((
        np.array([delta_p, delta_pf], dtype=np.float32),
        diff_metrics,
        post_metrics[:18]
    ))

    if features.size < target_dim:
        features = np.pad(features, (0, target_dim - features.size), mode="constant")
    elif features.size > target_dim:
        features = features[:target_dim]

    return features.astype(np.float32)


def extract_40d_features(
    raw_samples: list[float],
    p_before: float,
    p_after: float,
    pf_before: float,
    pf_after: float
) -> np.ndarray:
    return extract_features(raw_samples, p_before, p_after, pf_before, pf_after, target_dim=40)


def extract_22d_features(
    raw_samples: list[float],
    p_before: float,
    p_after: float,
    pf_before: float,
    pf_after: float
) -> np.ndarray:
    return extract_features(raw_samples, p_before, p_after, pf_before, pf_after, target_dim=22)