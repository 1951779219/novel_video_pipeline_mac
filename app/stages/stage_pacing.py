"""图音配比：把 segments 按音频时长/句数/段落/固定数 分配到图片组。
返回 list[ImagePlan]：每个 ImagePlan 包含若干 segments，对应一张图。
"""
from dataclasses import dataclass, field

from app.stages.stage2_clean import Segment


@dataclass
class ImagePlan:
    image_index: int
    segments: list[Segment] = field(default_factory=list)
    duration: float = 0.0  # 累计音频秒数
    highlight_segment_indexes: list[int] = field(default_factory=list)
    highlight_text: str = ""
    highlight_people: list[str] = field(default_factory=list)
    highlight_location: str = ""
    highlight_action: str = ""

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)


def plan_images(
    segments: list[Segment],
    durations: list[float],
    mode: str = "by_duration",
    seconds_per_image: float = 6.0,
    sentences_per_image: int = 3,
    fixed_count: int = 10,
) -> list[ImagePlan]:
    """durations[i] 对应 segments[i] 的音频时长（秒）。"""
    assert len(segments) == len(durations)
    if not segments:
        return []

    plans: list[ImagePlan] = []

    if mode == "by_paragraph":
        # 一段（segment）一图
        for i, (seg, d) in enumerate(zip(segments, durations)):
            plans.append(ImagePlan(image_index=i, segments=[seg], duration=d))
        return plans

    if mode == "by_sentence":
        n = max(1, int(sentences_per_image))
        cur = ImagePlan(image_index=0)
        for seg, d in zip(segments, durations):
            cur.segments.append(seg)
            cur.duration += d
            if len(cur.segments) >= n:
                plans.append(cur)
                cur = ImagePlan(image_index=len(plans))
        if cur.segments:
            plans.append(cur)
        return plans

    if mode == "fixed_count":
        total = sum(durations)
        n = max(1, int(fixed_count))
        slot = total / n
        cur = ImagePlan(image_index=0)
        for seg, d in zip(segments, durations):
            cur.segments.append(seg)
            cur.duration += d
            if cur.duration >= slot and len(plans) < n - 1:
                plans.append(cur)
                cur = ImagePlan(image_index=len(plans))
        if cur.segments:
            plans.append(cur)
        return plans

    # 默认 by_duration
    target = max(1.0, float(seconds_per_image))
    cur = ImagePlan(image_index=0)
    for seg, d in zip(segments, durations):
        cur.segments.append(seg)
        cur.duration += d
        if cur.duration >= target:
            plans.append(cur)
            cur = ImagePlan(image_index=len(plans))
    if cur.segments:
        plans.append(cur)
    return plans
