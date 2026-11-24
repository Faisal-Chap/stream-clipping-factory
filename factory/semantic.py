"""Optional local CLIP image-to-text retrieval on CPU."""

import hashlib
import importlib.util

from .media import MODEL

MODEL_REVISION = "6b7c523703ae26e39be4b03774966f8571c7f523"
FILES = ("tokenizer.json", "text_model_quantized.onnx", "vision_model_quantized.onnx")
PROMPTS = (
    ("driving", "a Grand Theft Auto car driving down a street"),
    ("vehicle damage", "a vehicle collision or an explosion in Grand Theft Auto gameplay"),
    ("on-foot combat", "a shootout with armed characters in Grand Theft Auto gameplay"),
    ("on-foot scene", "people standing or walking in open world video game gameplay"),
    ("aircraft", "a helicopter or airplane in Grand Theft Auto gameplay"),
    ("conversation", "video game characters talking in a cutscene"),
    ("interface", "a paused game, loading screen, menu or game map"),
)


def availability():
    return all((MODEL / name).is_file() for name in FILES) and all(
        importlib.util.find_spec(module) for module in ("numpy", "onnxruntime", "tokenizers"))


class ClipRetriever:
    def __init__(self):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.vision = ort.InferenceSession(str(MODEL / FILES[2]), options, ["CPUExecutionProvider"])
        text = ort.InferenceSession(str(MODEL / FILES[1]), options, ["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(str(MODEL / FILES[0]))
        tokenizer.enable_padding(pad_id=49407)
        encoded = tokenizer.encode_batch(["a screenshot of " + prompt for _, prompt in PROMPTS])
        inputs = {"input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                  "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64)}
        self.text = self.normalized(text.run(["text_embeds"],
                                            {i.name: inputs[i.name] for i in text.get_inputs()})[0])

    def normalized(self, vectors):
        return vectors / self.np.maximum(self.np.linalg.norm(vectors, axis=1, keepdims=True), 1e-8)

    def labels(self, frames, width, height):
        np = self.np
        # Preserve the whole frame by letterboxing it before applying CLIP's RGB normalization.
        pixels = np.frombuffer(frames, dtype=np.uint8).reshape(-1, height, width, 3)
        pixels = pixels[:, np.arange(224) * height // 224][:, :, np.arange(224) * width // 224]
        pixels = pixels.astype(np.float32) / 255.0
        pixels = (pixels - np.array([.48145466, .4578275, .40821073], np.float32)) / np.array(
            [.26862954, .26130258, .27577711], np.float32)
        features = self.vision.run(["image_embeds"], {"pixel_values": pixels.transpose(0, 3, 1, 2)})[0]
        similarities = self.normalized(features) @ self.text.T
        order = similarities.argsort(axis=1)
        return [(PROMPTS[best][0], float(row[best] - row[second]))
                for row, (second, best) in zip(similarities, order[:, -2:], strict=True)]


def model_fingerprint():
    digest = hashlib.sha256()
    for name in FILES:
        with (MODEL / name).open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()[:16]
