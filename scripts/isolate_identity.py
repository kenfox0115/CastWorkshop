"""CPU-only person isolation for multi-reference identity images."""
import argparse
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort


def isolate(model_path, input_path, output_path):
    bgr = cv2.imread(str(input_path))
    if bgr is None:
        raise ValueError(f"Could not read identity image: {input_path}")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    image = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    image = (image - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    image = np.transpose(image, (2, 0, 1))[None]
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    prediction = session.run(None, {session.get_inputs()[0].name: image})[0][0, 0]
    prediction = (prediction - prediction.min()) / (prediction.max() - prediction.min() + 1e-8)
    mask = cv2.resize(prediction, (width, height), interpolation=cv2.INTER_LANCZOS4)
    mask = np.clip((mask - 0.08) / 0.84, 0, 1)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.5)[..., None]
    neutral = np.full_like(rgb, 128)
    result = (rgb * mask + neutral * (1 - mask)).astype(np.uint8)
    if not cv2.imwrite(str(output_path), cv2.cvtColor(result, cv2.COLOR_RGB2BGR)):
        raise OSError(f"Could not write isolated identity image: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    isolate(args.model, args.input, args.output)
