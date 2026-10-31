"""Focus-stack merging: align the frames of a stack and keep each pixel's sharpest one.

Frames are aligned to the middle frame with ECC (affine, since focus breathing
changes magnification), chained through neighbours, on a downscaled grey copy.
They are then fused band by band in a Laplacian pyramid: in every band each
frame is weighted by its own local contrast in that band, raised to a high
power. Fine bands follow fine detail, coarse bands follow coarse contrast, so
the blur a defocused foreground spreads over a sharp background loses to the
frame without it, and seams are blended per band instead of cut.

Frames are streamed: every pass decodes one frame at a time, so memory stays
at a few full-size buffers no matter how many frames the stack has.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import rawpy

from .media import RAW_EXTENSIONS

ALIGN_WIDTH = 1500  # alignment runs on a copy this wide
SHARPNESS_POWER = 6.0
# Per pyramid level, how far (in that level's px) band contrast is pooled
POOL_SIGMA = (4.0, 3.0, 2.0)
DEPTH_LEVEL = 2
MIN_ECC = 0.6


@dataclass
class FrameAlignment:
    path: Path
    warp: np.ndarray  # 2x3, maps middle-frame coords to this frame's coords
    method: str  # "reference", "ecc", "orb" or "none"
    score: float  # ECC correlation with its neighbour (1.0 for the reference)

    @property
    def scale(self) -> float:
        return float(np.sqrt(abs(np.linalg.det(self.warp[:, :2]))))

    @property
    def shift(self) -> Tuple[float, float]:
        return float(self.warp[0, 2]), float(self.warp[1, 2])


@dataclass
class MergeResult:
    image: np.ndarray  # uint16 RGB, cropped
    depth: np.ndarray  # uint8 frame index per pixel, cropped, full res
    alignments: List[FrameAlignment] = field(default_factory=list)
    crop: Tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, width, height


def _raw_brightness(path: Path) -> float:
    """One brightness gain for the whole stack, dcraw-style, from one frame.

    rawpy's auto-brightness is per image, which would give every frame a
    slightly different exposure and make the fusion seams visible.
    """
    with rawpy.imread(str(path)) as raw:
        linear = raw.postprocess(
            output_bps=16,
            use_camera_wb=True,
            no_auto_bright=True,
            gamma=(1, 1),
            half_size=True,
        )
    white = max(float(np.percentile(linear[..., c], 99)) for c in range(3))
    return 65535.0 / white if white > 0 else 1.0


def decode(path: Path, bright: float = 1.0) -> np.ndarray:
    """Full-size 16-bit RGB with the camera white balance, orientation applied."""
    if path.suffix.lower() in RAW_EXTENSIONS:
        with rawpy.imread(str(path)) as raw:
            return raw.postprocess(
                output_bps=16, use_camera_wb=True, no_auto_bright=True, bright=bright
            )
    image = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH)
    if image is None:
        raise ValueError(f"cannot read {path}")
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return rgb if rgb.dtype == np.uint16 else rgb.astype(np.uint16) * 257


def _gray(rgb: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gray /= 65535.0
    return gray


def _small(gray: np.ndarray, scale: float) -> np.ndarray:
    size = (round(gray.shape[1] * scale), round(gray.shape[0] * scale))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)


def _to3(warp: np.ndarray) -> np.ndarray:
    return np.vstack([warp, [0.0, 0.0, 1.0]])


def _plausible(warp: np.ndarray, width: int) -> bool:
    scale = np.sqrt(abs(np.linalg.det(warp[:, :2])))
    return 0.85 < scale < 1.15 and float(np.abs(warp[:, 2]).max()) < width * 0.15


def _ecc(template: np.ndarray, image: np.ndarray) -> Tuple[np.ndarray, float]:
    """2x3 warp taking template coords to image coords, and its correlation."""
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    score, warp = cv2.findTransformECC(
        template, image, warp, cv2.MOTION_AFFINE, criteria
    )
    return warp.astype(np.float64), float(score)


def _orb(template: np.ndarray, image: np.ndarray) -> Optional[np.ndarray]:
    def to8(g: np.ndarray) -> np.ndarray:
        low, high = float(g.min()), float(g.max())
        scaled = (g - low) * (255 / max(high - low, 1e-6))
        return np.clip(scaled, 0, 255).astype(np.uint8)

    orb = cv2.ORB.create(4000)
    kt, dt = orb.detectAndCompute(to8(template), None)
    ki, di = orb.detectAndCompute(to8(image), None)
    if dt is None or di is None:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(dt, di)
    if len(matches) < 12:
        return None
    src = np.array([kt[m.queryIdx].pt for m in matches], dtype=np.float32)
    dst = np.array([ki[m.trainIdx].pt for m in matches], dtype=np.float32)
    warp, _ = cv2.estimateAffine2D(src, dst, ransacReprojThreshold=2.0)
    return warp


def _pair_warp(
    template: np.ndarray, image: np.ndarray
) -> Tuple[np.ndarray, str, float]:
    width = template.shape[1]
    try:
        warp, score = _ecc(template, image)
        if score >= MIN_ECC and _plausible(warp, width):
            return warp, "ecc", score
    except cv2.error:
        score = 0.0
    orb = _orb(template, image)
    if orb is not None and _plausible(orb, width):
        return orb, "orb", score
    return np.eye(2, 3), "none", score


def align(
    smalls: Sequence[np.ndarray], paths: Sequence[Path], scale: float, middle: int
) -> List[FrameAlignment]:
    """Full-res warps of every frame to the middle one.

    Each frame is aligned to its neighbour on the way to the middle (the
    neighbour has a similar focus distance, so it looks most alike) and the
    pairwise warps are chained.
    """
    totals: List[Optional[np.ndarray]] = [None] * len(smalls)
    totals[middle] = np.eye(3)
    results: List[Optional[FrameAlignment]] = [None] * len(smalls)
    results[middle] = FrameAlignment(paths[middle], np.eye(2, 3), "reference", 1.0)
    order = [*range(middle + 1, len(smalls)), *range(middle - 1, -1, -1)]
    for i in order:
        j = i - 1 if i > middle else i + 1
        pair, method, score = _pair_warp(smalls[j], smalls[i])
        previous = totals[j]
        assert previous is not None
        total = _to3(pair) @ previous
        totals[i] = total
        full = total[:2].copy()
        full[:, 2] /= scale
        results[i] = FrameAlignment(paths[i], full, method, score)
    return [r for r in results if r is not None]


def _warp(image: np.ndarray, warp: np.ndarray) -> np.ndarray:
    if np.allclose(warp, np.eye(2, 3)):
        return image
    return cv2.warpAffine(
        image,
        warp,
        (image.shape[1], image.shape[0]),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _bands(image: np.ndarray, levels: int):
    """Laplacian pyramid, finest band first and the low-pass base last.

    Each band is a fresh array the caller may modify in place.
    """
    for _ in range(levels):
        down = cv2.pyrDown(image)
        up = cv2.pyrUp(down, dstsize=(image.shape[1], image.shape[0]))
        np.subtract(image, up, out=up)
        yield up
        image = down
    yield image


def _energy(band: np.ndarray, level: int) -> np.ndarray:
    """Local contrast of one grey band, pooled over a few pixels."""
    sigma = POOL_SIGMA[min(level, len(POOL_SIGMA) - 1)]
    return cv2.GaussianBlur(np.abs(band), (0, 0), sigma)


def _valid_crop(
    alignments: Sequence[FrameAlignment], shape: Tuple[int, int]
) -> Tuple[int, int, int, int]:
    """Largest rectangle every warped frame covers (alignment leaves borders)."""
    height, width = shape
    scale = min(1.0, 1000 / width)
    small = (round(width * scale), round(height * scale))
    valid = np.ones((small[1], small[0]), dtype=np.uint8)
    for a in alignments:
        warp = a.warp.copy()
        warp[:, 2] *= scale
        valid &= cv2.warpAffine(
            np.ones_like(valid),
            warp,
            small,
            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
            borderValue=0,
        )
    top, bottom, left, right = 0, valid.shape[0], 0, valid.shape[1]
    while top < bottom and left < right:
        box = valid[top:bottom, left:right]
        if box.all():
            break
        # Shrink the side with the most uncovered pixels
        misses = [
            (int((box[0] == 0).sum()), "top"),
            (int((box[-1] == 0).sum()), "bottom"),
            (int((box[:, 0] == 0).sum()), "left"),
            (int((box[:, -1] == 0).sum()), "right"),
        ]
        side = max(misses)[1]
        if side == "top":
            top += 1
        elif side == "bottom":
            bottom -= 1
        elif side == "left":
            left += 1
        else:
            right -= 1
    margin = 2
    x0 = min(width, int(np.ceil(left / scale)) + margin)
    y0 = min(height, int(np.ceil(top / scale)) + margin)
    x1 = max(x0, int(np.floor(right / scale)) - margin)
    y1 = max(y0, int(np.floor(bottom / scale)) - margin)
    return x0, y0, x1 - x0, y1 - y0


def _levels(shape: Tuple[int, int]) -> int:
    return max(1, int(np.log2(min(shape) / 16)))


def merge(
    paths: Sequence[Path],
    middle: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> MergeResult:
    """Merge the frames (in focus order) into one image sharp front to back."""
    if len(paths) < 2:
        raise ValueError("a focus stack needs at least two frames")
    say = progress or (lambda _: None)
    middle = (len(paths) - 1) // 2 if middle is None else middle
    raw = paths[middle].suffix.lower() in RAW_EXTENSIONS
    bright = _raw_brightness(paths[middle]) if raw else 1.0

    say("aligning")
    smalls = []
    shape: Tuple[int, int] = (0, 0)
    scale = 1.0
    for path in paths:
        gray = _gray(decode(path, bright))
        if shape == (0, 0):
            shape = (gray.shape[0], gray.shape[1])
            scale = min(1.0, ALIGN_WIDTH / shape[1])
        elif gray.shape != shape:
            raise ValueError(f"{path.name} is {gray.shape}, expected {shape}")
        smalls.append(cv2.GaussianBlur(_small(gray, scale), (0, 0), 1.0))
    alignments = align(smalls, list(paths), scale, middle)
    del smalls

    levels = _levels(shape)
    depth_level = min(DEPTH_LEVEL, levels - 1)

    # Pass 1: per pyramid level, the normaliser for every frame's weights
    say("measuring sharpness")
    totals: List[np.ndarray] = []
    norms: List[float] = []
    best: Optional[np.ndarray] = None
    depth: Optional[np.ndarray] = None
    for index, a in enumerate(alignments):
        gray = _gray(_warp(decode(a.path, bright), a.warp))
        for level, band in enumerate(_bands(gray, levels)):
            if level == levels:
                break
            energy = _energy(band, level)
            if index == 0:
                norms.append(float(np.percentile(energy, 99.9)) or 1.0)
            energy /= norms[level]
            weight = np.power(energy, SHARPNESS_POWER) + 1e-20
            if index == 0:
                totals.append(weight)
            else:
                totals[level] += weight
            if level == depth_level:
                if best is None or depth is None:
                    best = energy.copy()
                    depth = np.zeros(energy.shape, dtype=np.uint8)
                better = energy > best
                best[better] = energy[better]
                depth[better] = index
        del gray
    del best
    assert depth is not None

    # Pass 2: every frame's bands, weighted by its share of each band's contrast
    say("fusing")
    fused: List[np.ndarray] = []
    for index, a in enumerate(alignments):
        image = _warp(decode(a.path, bright), a.warp).astype(np.float32)
        gray_bands = _bands(_gray(image), levels)
        bands = _bands(image, levels)
        del image
        weight = np.ones(1, dtype=np.float32)
        for level, band in enumerate(bands):
            if level < levels:
                weight = _energy(next(gray_bands), level)
                weight /= norms[level]
                np.power(weight, SHARPNESS_POWER, out=weight)
                weight += 1e-20
                weight /= totals[level]
            else:
                # The low-pass base follows the coarsest band's choice
                weight = cv2.pyrDown(weight)
            band *= weight[..., None]
            if index == 0:
                fused.append(band)
            else:
                fused[level] += band
            del band
    del totals

    result = fused.pop()
    while fused:
        band = fused.pop()
        result = cv2.pyrUp(result, dstsize=(band.shape[1], band.shape[0]))
        np.add(result, band, out=result)
        del band
    np.clip(result, 0, 65535, out=result)
    merged = result.astype(np.uint16)
    del result
    depth = cv2.resize(depth, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)

    x, y, w, h = _valid_crop(alignments, shape)
    return MergeResult(
        image=merged[y : y + h, x : x + w],
        depth=depth[y : y + h, x : x + w],
        alignments=alignments,
        crop=(x, y, w, h),
    )


def write_outputs(
    result: MergeResult, base: Path, frames: int
) -> Tuple[Path, Path, Path]:
    """<base>.tif (16-bit), <base>.jpg (quality 95) and <base>_depth.png."""
    base.parent.mkdir(parents=True, exist_ok=True)
    bgr = cv2.cvtColor(result.image, cv2.COLOR_RGB2BGR)
    tif = base.with_suffix(".tif")
    jpg = base.with_suffix(".jpg")
    png = base.with_name(base.name + "_depth.png")
    if not cv2.imwrite(str(tif), bgr):
        raise OSError(f"could not write {tif}")
    eight = np.round(bgr.astype(np.float32) / 257.0).astype(np.uint8)
    if not cv2.imwrite(str(jpg), eight, [cv2.IMWRITE_JPEG_QUALITY, 95]):
        raise OSError(f"could not write {jpg}")
    scale = min(1.0, 1200 / result.depth.shape[1])
    depth = cv2.resize(
        result.depth,
        (round(result.depth.shape[1] * scale), round(result.depth.shape[0] * scale)),
        interpolation=cv2.INTER_NEAREST,
    )
    levels = np.round(depth.astype(np.float32) * 255 / max(1, frames - 1))
    colored = cv2.applyColorMap(levels.astype(np.uint8), cv2.COLORMAP_VIRIDIS)
    if not cv2.imwrite(str(png), colored):
        raise OSError(f"could not write {png}")
    return tif, jpg, png
