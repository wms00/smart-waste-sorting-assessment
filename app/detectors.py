"""YOLO 适配器骨架。模型加载代码已给出，推理结果转换由学员完成。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .protocol import WasteType


@dataclass
class Detection:
    waste_type: WasteType
    class_name: str
    confidence: float
    box: Tuple[int, int, int, int]


class Detector(ABC):
    @abstractmethod
    def detect(self, frame: np.ndarray) -> List[Detection]:
        raise NotImplementedError


class YoloDetector(Detector):
    """加载指定权重，并把模型原始类别映射到四类垃圾。"""

    DEFAULT_CLASS_MAP = {
        "bottolad": WasteType.RECYCLABLE,
        "plasticbottle": WasteType.RECYCLABLE,
        "box": WasteType.RECYCLABLE,
        "can": WasteType.RECYCLABLE,
        "papercup": WasteType.RECYCLABLE,
        "yangleduo": WasteType.RECYCLABLE,
        "potato": WasteType.KITCHEN,
        "carrot": WasteType.KITCHEN,
        "qincarrot": WasteType.KITCHEN,
        "redcarrot": WasteType.KITCHEN,
        "whitcarrot": WasteType.KITCHEN,
        "battery": WasteType.HAZARDOUS,
        "drug": WasteType.HAZARDOUS,
        "cobblestone": WasteType.OTHER,
        "porcelain": WasteType.OTHER,
    }

    def __init__(
        self,
        weights: Optional[str] = None,
        model=None,
        confidence: float = 0.50,
        device: Optional[str] = None,
    ) -> None:
        if not 0.0 < confidence <= 1.0:
            raise ValueError("置信度阈值必须在 (0, 1] 范围内")
        if model is None:
            if not weights:
                raise ValueError("必须指定 YOLO 权重文件")
            weight_path = Path(weights)
            if not weight_path.is_file():
                raise FileNotFoundError("YOLO 权重不存在：{}".format(weight_path))
            try:
                import torch
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError("未安装 YOLO 运行环境") from exc
            self.device = device or ("cuda:0" if torch.cuda.is_available() else "cpu")
            self.model = YOLO(str(weight_path))
        else:
            self.model = model
            self.device = device or "cpu"

        self.confidence = confidence
        model_names = getattr(self.model, "names", {})
        unknown = sorted(set(model_names.values()) - set(self.DEFAULT_CLASS_MAP))
        if unknown:
            raise ValueError("权重中存在未配置类别：{}".format(", ".join(unknown)))

    def detect(self, frame: np.ndarray) -> List[Detection]:
        """调用 ``model.predict`` 并把 boxes 转换为内部 Detection 列表。"""
        # 必须用 source= 关键字传入画面（公开测试会检查这个参数）
        results = self.model.predict(
            source=frame,
            conf=self.confidence,
            device=self.device,
            verbose=False,
        )
        if not results:
            return []

        result = results[0]
        # 真实权重和测试用的 FakeYolo 都提供 names 映射；优先取 result 上的
        names = getattr(result, "names", None) or getattr(self.model, "names", {})
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return []

        detections = []
        for box in boxes:
            # .item() 把单元素张量转成 Python 数字；CUDA 张量会自动同步回 CPU
            confidence_value = float(box.conf.item())
            class_id = int(box.cls.item())
            class_name = names.get(class_id, str(class_id))

            # 未知类别直接跳过（__init__ 已校验过权重类别，这里是防御性兜底）
            if class_name not in self.DEFAULT_CLASS_MAP:
                continue
            if confidence_value < self.confidence:
                continue

            # xyxy 是 (1,4) 张量；[0] 取第一行，.cpu().numpy() 转成 numpy 数组
            xyxy = box.xyxy[0].cpu().numpy()
            x1, y1, x2, y2 = (int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3]))

            detections.append(
                Detection(
                    waste_type=self.DEFAULT_CLASS_MAP[class_name],
                    class_name=class_name,
                    confidence=confidence_value,
                    box=(x1, y1, x2, y2),
                )
            )

        # 按置信度从高到低排序，调用方取第一个即得“最可信目标”
        detections.sort(key=lambda detection: detection.confidence, reverse=True)
        return detections
