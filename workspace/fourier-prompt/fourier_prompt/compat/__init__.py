"""Compatibility layer between ``fourier-prompt`` and the ``fourier-brushstroke`` skill.

The only module here is :mod:`fourier_prompt.compat.skill_api`; it is the single
place allowed to resolve the skill directory, inject ``scripts/`` into
``sys.path`` and re-export the public skill API.
"""

from .skill_api import SkillApiError, skill_dir

__all__ = ["SkillApiError", "skill_dir"]
