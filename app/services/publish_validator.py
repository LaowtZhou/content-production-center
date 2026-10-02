"""发布准备产物的自动验收。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PIL import Image

from app.config import get_setting


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _validate_wechat_scan_layout(text: str) -> dict[str, Any]:
    """检查公众号稿是否具备最小扫读层，而不是只有连续普通段落。"""
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
    headings = re.findall(r"^##+\s+", body, flags=re.MULTILINE)
    emphasis = re.findall(r"(?<!\*)\*\*[^*\n]+\*\*", body)
    quotes = re.findall(r"^>\s+", body, flags=re.MULTILINE)
    list_items = re.findall(r"^(?:[-*]|\d+\.)\s+", body, flags=re.MULTILINE)
    checks = {
        "section_headings": len(headings),
        "emphasis_segments": len(emphasis),
        "quote_blocks": len(quotes),
        "list_items": len(list_items),
    }
    errors: list[str] = []
    if len(headings) < 2:
        errors.append("公众号稿缺少足够的分节标题")
    if len(emphasis) < 3:
        errors.append("公众号稿缺少核心判断加粗，无法支持手机扫读")
    if not quotes and len(list_items) < 3:
        errors.append("公众号稿缺少引用块或列表呼吸点")
    return {"ok": not errors, "errors": errors, "checks": checks}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_image_artifact(item: dict[str, Any]) -> bool:
    kind = str(item.get("kind") or "").lower()
    path = str(item.get("path") or "").lower()
    return path.endswith(tuple(IMAGE_SUFFIXES)) or any(token in kind for token in ("image", "cover", "配图", "封面"))


def validate_publish_bundle(
    vault_project_path: str | None,
    primary_article_path: str | None,
    artifacts: list[dict[str, Any]],
    self_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """检查公众号发布稿的文章、图片和 imagegen 记录。"""
    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    root_value = get_setting("content_vault_dir", "").strip()
    root = Path(root_value).resolve() if root_value else None
    project = Path(vault_project_path).resolve() if vault_project_path else None
    article = Path(primary_article_path).resolve() if primary_article_path else None

    if not root:
        errors.append("未配置内容 vault 根目录")
    if not project or not project.is_dir():
        errors.append("内容项目目录不存在")
    elif root and not _inside(project, root):
        errors.append("内容项目目录不在内容 vault 内")
    if not article or not article.is_file():
        errors.append("文章主稿不存在")
    elif root and not _inside(article, root):
        errors.append("文章主稿不在内容 vault 内")
    else:
        try:
            text = article.read_text(encoding="utf-8-sig").strip()
            if not text:
                errors.append("文章主稿为空")
            checks.append({"kind": "article_content", "path": str(article), "ok": bool(text)})
            scan_layout = _validate_wechat_scan_layout(text)
            checks.append({"kind": "wechat_scan_layout", **scan_layout})
            errors.extend(scan_layout["errors"])
        except (OSError, UnicodeError) as exc:
            errors.append(f"文章主稿无法读取: {exc}")

    image_items = [item for item in artifacts if isinstance(item, dict) and _is_image_artifact(item)]
    if not image_items:
        errors.append("未回传任何图片产物")
    if not any("cover" in str(item.get("kind") or "").lower() or "封面" in str(item.get("kind") or "") for item in image_items):
        errors.append("未回传封面图片")

    for item in image_items:
        raw_path = str(item.get("path") or "")
        path = Path(raw_path).resolve() if raw_path else None
        item_errors: list[str] = []
        generator = str(item.get("generator") or "").strip().lower()
        if not path or not path.is_file():
            item_errors.append("文件不存在")
        elif root and not _inside(path, root):
            item_errors.append("文件不在内容 vault 内")
        elif path.suffix.lower() not in IMAGE_SUFFIXES:
            item_errors.append("不是支持的 PNG/JPEG/WebP 图片")
        else:
            if generator != "imagegen":
                item_errors.append("图片未声明由 imagegen 生成")
            if not (str(item.get("generation_id") or "").strip() or str(item.get("prompt_hash") or "").strip()):
                item_errors.append("缺少 imagegen 生成记录")
            if path.stat().st_size < 1024:
                item_errors.append("图片文件过小，疑似占位图")
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    width, height = image.size
                    ratio = width / height if height else 0
                    if width < 800 or height < 450:
                        item_errors.append(f"尺寸过小: {width}x{height}")
                    if ratio < 1 / 3 or ratio > 3:
                        item_errors.append(f"比例异常: {width}x{height}")
                    checks.append({
                        "kind": str(item.get("kind") or "image"),
                        "path": str(path),
                        "generator": generator,
                        "dimensions": f"{width}x{height}",
                        "ok": not item_errors,
                    })
            except (OSError, SyntaxError, ValueError) as exc:
                item_errors.append(f"图片无法解码: {exc}")
        if item_errors:
            errors.append(f"{raw_path or '未命名图片'}：" + "；".join(item_errors))

    generation_check = (self_check or {}).get("image_generation")
    if not isinstance(generation_check, dict) or str(generation_check.get("mode") or "").lower() != "imagegen":
        errors.append("自检记录未确认 imagegen")
    else:
        if generation_check.get("reviewed") is not True:
            errors.append("未记录图片生成后的发布前复核")
        if str(generation_check.get("quality") or "").lower() not in {"pass", "passed", "通过"}:
            errors.append("图片发布前复核结论不是通过")

    return {
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "image_count": len(image_items),
    }
