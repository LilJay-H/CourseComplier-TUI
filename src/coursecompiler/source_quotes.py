"""Complete quotation policies for legacy authoring and the shared extractor.

The lightweight extractor is shared with the private cloud; composer/Agent
orchestration stays in the internal application and is not a server dependency.
"""
from __future__ import annotations
from pathlib import Path
from .source_excerpt import QUOTE_POLICY, _Quotes, check_extract_quotes


def check_complete_quotes(text: str, root: Path, sources: dict) -> tuple[list[str], list[str]]:
    checks = sources.get('output_checks') or {}
    if not isinstance(checks, dict):
        return ['无效的 output_checks 配置；请刷新交接任务'], []
    from .composer import POLICY as COMPOSED_POLICY
    if checks.get('complete_quotes') == COMPOSED_POLICY:
        from .composer import check_composed_quotes
        from .authoring_stages import PLAN_INPUT, read_object, validate_plan, latest_plan
        try:
            plan = (validate_plan(root, sources, read_object(root, PLAN_INPUT))
                    if (root / PLAN_INPUT).exists() else latest_plan(root, sources))
            if plan is None:
                raise ValueError('COMPOSED_PLAN_REQUIRED')
            from .composition_store import checked_composition
            checked_composition(root, sources, text.encode('utf-8'), 'complete', plan)
            return check_composed_quotes(text, root, sources, plan['objects']), [
                'Host 只认证注册范围的确定性节选；音频、上下文、标题和全稿语义仍待审阅']
        except (OSError, ValueError, KeyError):
            return ['COMPOSED_QUOTE_SOURCE_OR_PLAN_INVALID'], []
    return check_extract_quotes(text, root, sources)
