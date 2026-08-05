"""레시피(D:\\AIV\\MODEL\\<제품>\\<버전>) 파서.

설계도 모델을 구성한다:
    IMG -> ROI(baseimgidx) -> ALG(requireroiidx) -> DLMODEL(DLModel) -> 모델 파일/GPU

ini 형식: 섹션 [ALG0001] / key = value, 주석 ';'. 인코딩 UTF-8/CP949 혼용 방어.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class RecipeAlg:
    idx: int
    name: str = ""
    alg_id: str = ""
    roi_idx: list[int] = field(default_factory=list)
    dl_model: list[int] = field(default_factory=list)
    alg_type: str = ""
    dll: str = ""


@dataclass(slots=True)
class RecipeRoi:
    idx: int
    name: str = ""
    base_img: int = 0


@dataclass(slots=True)
class RecipeModel:
    idx: int
    name: str = ""
    model_file: str = ""
    dev_type: int = 0
    dev_index: int = 0
    instance_count: int = 1
    infer_dll: str = ""
    # v1.4: 인퍼런스 동작 플래그 (DeepLearningInspector.cpp 의 키와 기본값 동일)
    on_memory: int = 1        # "on memory infer" — 1=VRAM 상주, 0=요청 시 로드/언로드
    patch_infer: int = 0      # "use patch infer" — 1=패치 슬라이딩 인퍼런스
    athena_type: int = 0      # "AthenaModelType" — 2=SEG / 3=CLA / 11=DET
    alg_blocks: str = ""      # "alg blocks name" — 후처리 블록 그래프 JSON

    @property
    def task(self) -> str:
        return {2: "SEG", 3: "CLA", 11: "DET"}.get(self.athena_type, "")


@dataclass
class Recipe:
    root: str = ""
    version: str = ""
    alg_count: int = 0
    thread_count: int = 0
    model_path: str = ""
    algs: dict[int, RecipeAlg] = field(default_factory=dict)
    rois: dict[int, RecipeRoi] = field(default_factory=dict)
    models: dict[int, RecipeModel] = field(default_factory=dict)

    def alg_name(self, idx: int) -> str:
        a = self.algs.get(idx)
        return a.name if a else f"ALG{idx:04d}"

    def alg_models(self, idx: int) -> list[RecipeModel]:
        a = self.algs.get(idx)
        if not a:
            return []
        return [self.models[m] for m in a.dl_model if m in self.models]


def _read_ini(path: str) -> Optional[configparser.ConfigParser]:
    if not os.path.exists(path):
        return None
    cp = configparser.ConfigParser(strict=False, delimiters=("=",),
                                   comment_prefixes=(";", "#"), interpolation=None)
    for enc in ("utf-8-sig", "cp949"):
        try:
            with open(path, "r", encoding=enc) as f:
                cp.read_file(f)
            return cp
        except UnicodeDecodeError:
            continue
        except configparser.Error:
            return None
    return None


def _ints(val: str) -> list[int]:
    """"1" / "1,3" / "1 , 3" -> [1, 3]"""
    out = []
    for tok in re.split(r"[,\s]+", val.strip()):
        if tok.isdigit():
            out.append(int(tok))
    return out


def find_version_dir(recipe_root: str, hint: str = "") -> str:
    """제품 폴더 아래 버전(타임스탬프) 폴더 선택. hint 가 있으면 우선 매칭, 없으면 최신."""
    if os.path.exists(os.path.join(recipe_root, "ALG.ini")):
        return recipe_root  # 이미 버전 폴더를 직접 받은 경우
    subs = [d for d in os.listdir(recipe_root)
            if os.path.isdir(os.path.join(recipe_root, d))
            and os.path.exists(os.path.join(recipe_root, d, "ALG.ini"))]
    if not subs:
        raise FileNotFoundError(f"ALG.ini 를 찾을 수 없습니다: {recipe_root}")
    if hint:
        for d in subs:
            if hint in d:
                return os.path.join(recipe_root, d)
    return os.path.join(recipe_root, sorted(subs)[-1])


def load_recipe(recipe_root: str, version_hint: str = "") -> Recipe:
    vdir = find_version_dir(recipe_root, version_hint)
    r = Recipe(root=recipe_root, version=os.path.basename(vdir))

    alg = _read_ini(os.path.join(vdir, "ALG.ini"))
    if alg:
        info = alg["ALG Information"] if alg.has_section("ALG Information") else {}
        r.alg_count = int(info.get("alg count", "0") or 0)
        r.thread_count = int(info.get("thread count", "0") or 0)
        for sec in alg.sections():
            m = re.match(r"^ALG(\d+)$", sec)
            if not m:
                continue
            idx = int(m.group(1))
            s = alg[sec]
            r.algs[idx] = RecipeAlg(
                idx=idx,
                name=s.get("Name", "").strip(),
                alg_id=s.get("ID", "").strip(),
                roi_idx=_ints(s.get("requireroiidx", "")),
                dl_model=_ints(s.get("DLModel", "")),
                alg_type=s.get("AlgorithmType", "").strip(),
                dll=s.get("DLL", "").strip(),
            )

    roi = _read_ini(os.path.join(vdir, "ROI.ini"))
    if roi:
        for sec in roi.sections():
            m = re.match(r"^ROI(\d+)$", sec)
            if not m:
                continue
            idx = int(m.group(1))
            s = roi[sec]
            r.rois[idx] = RecipeRoi(idx=idx, name=s.get("name", "").strip(),
                                    base_img=int(s.get("baseimgidx", "0") or 0))

    dlm = _read_ini(os.path.join(vdir, "DLMODEL.ini"))
    if dlm:
        info = dlm["DLMODEL Information"] if dlm.has_section("DLMODEL Information") else {}
        r.model_path = info.get("dlmodel path", "").strip()
        for sec in dlm.sections():
            m = re.match(r"^DLMODEL(\d+)$", sec)
            if not m:
                continue
            idx = int(m.group(1))
            s = dlm[sec]
            r.models[idx] = RecipeModel(
                idx=idx,
                name=s.get("name", "").strip(),
                model_file=s.get("model name", "").strip(),
                dev_type=int(s.get("dev type", "0") or 0),
                dev_index=int(s.get("dev index", "0") or 0),
                instance_count=int(s.get("instance count", "1") or 1),
                infer_dll=s.get("infer dll name", "").strip(),
                on_memory=int(s.get("on memory infer", "1") or 1),
                patch_infer=int(s.get("use patch infer", "0") or 0),
                athena_type=int(s.get("athenamodeltype", "0") or 0),
                alg_blocks=s.get("alg blocks name", "").strip(),
            )
    return r
