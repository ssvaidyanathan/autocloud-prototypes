import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class SkillMetadata:
    """Metadata describing an agent skill for progressive disclosure."""

    name: str
    description: str
    path: Path
    references: List[str] = field(default_factory=list)


@dataclass
class SkillContent:
    """Full skill content loaded on demand."""

    metadata: SkillMetadata
    instructions: str
    reference_contents: Dict[str, str] = field(default_factory=dict)


class SkillsLoader:
    """Discovers, validates, and progressively loads Agent Skills."""

    def __init__(self, skills_dir: Optional[Union[str, Path]] = None) -> None:
        if skills_dir is None:
            # Default to agent/skills relative to this file's package root
            base_path = Path(__file__).resolve().parent.parent / "skills"
            self.skills_dir = base_path
        else:
            self.skills_dir = Path(skills_dir).resolve()

        self._metadata_cache: Dict[str, SkillMetadata] = {}
        self._skills_mapping: Dict[str, List[str]] = {}
        self._loaded_skills: Dict[str, SkillContent] = {}

    def discover_skills(self) -> Dict[str, SkillMetadata]:
        """Scan skills directory and parse frontmatter without loading full bodies."""
        if not self.skills_dir.exists() or not self.skills_dir.is_dir():
            return {}

        self._metadata_cache.clear()

        for skill_path in sorted(self.skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue

            # Security Guardrail: Reject any skill containing a scripts/ directory
            scripts_dir = skill_path / "scripts"
            if scripts_dir.exists() and scripts_dir.is_dir():
                raise ValueError(
                    f"Security violation: skill at '{skill_path.name}' contains a forbidden "
                    "'scripts/' directory. Code execution inside skills is prohibited."
                )

            skill_md = skill_path / "SKILL.md"
            if not skill_md.exists():
                continue

            metadata = self._parse_skill_metadata(skill_md, skill_path)
            self._metadata_cache[metadata.name] = metadata

        return self._metadata_cache

    def _parse_skill_metadata(self, skill_file: Path, skill_dir: Path) -> SkillMetadata:
        """Parse and validate frontmatter from SKILL.md."""
        content = skill_file.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(content)
        if not match:
            raise ValueError(
                f"Invalid skill file: '{skill_file}' must contain YAML "
                "frontmatter delimited by '---'"
            )

        frontmatter_str = match.group(1)
        try:
            parsed: Any = yaml.safe_load(frontmatter_str)
        except Exception as exc:
            raise ValueError(f"Failed to parse YAML frontmatter in '{skill_file}': {exc}") from exc

        if not isinstance(parsed, dict):
            raise ValueError(
                f"Frontmatter in '{skill_file}' must be a YAML mapping, got {type(parsed).__name__}"
            )

        name = parsed.get("name")
        description = parsed.get("description")

        # Validate name
        if not name or not isinstance(name, str):
            raise ValueError(f"Skill in '{skill_file}' missing required string field 'name'")
        name = name.strip()
        if len(name) > 64:
            raise ValueError(f"Skill name '{name}' exceeds maximum length of 64 characters")
        if not KEBAB_CASE_RE.match(name):
            raise ValueError(
                f"Skill name '{name}' must be kebab-case (lowercase alphanumeric with hyphens)"
            )

        # Validate description
        if not description or not isinstance(description, str) or not description.strip():
            raise ValueError(f"Skill in '{skill_file}' missing required non-empty 'description'")
        description = description.strip()

        # Discover reference files without loading content (progressive disclosure)
        references: List[str] = []
        refs_dir = skill_dir / "references"
        if refs_dir.exists() and refs_dir.is_dir():
            for ref_file in sorted(refs_dir.glob("*.md")):
                references.append(ref_file.name)

        return SkillMetadata(
            name=name,
            description=description,
            path=skill_dir,
            references=references,
        )

    def list_skills(self) -> List[SkillMetadata]:
        """List summary descriptions of all available skills (progressive disclosure)."""
        if not self._metadata_cache:
            self.discover_skills()
        return list(self._metadata_cache.values())

    def get_skill_metadata(self, name: str) -> Optional[SkillMetadata]:
        """Get skill metadata by name."""
        if not self._metadata_cache:
            self.discover_skills()
        return self._metadata_cache.get(name)

    def get_skill_content(self, name: str) -> SkillContent:
        """Load full skill instructions and reference documents on demand."""
        meta = self.get_skill_metadata(name)
        if meta is None:
            raise KeyError(f"Skill '{name}' not found")

        skill_md = meta.path / "SKILL.md"
        raw_content = skill_md.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(raw_content)
        instructions = match.group(2).strip() if match else raw_content.strip()

        ref_contents: Dict[str, str] = {}
        refs_dir = meta.path / "references"
        if refs_dir.exists() and refs_dir.is_dir():
            for ref_file in sorted(refs_dir.glob("*.md")):
                ref_contents[ref_file.name] = ref_file.read_text(encoding="utf-8").strip()

        skill_content = SkillContent(
            metadata=meta,
            instructions=instructions,
            reference_contents=ref_contents,
        )
        self._loaded_skills[name] = skill_content
        return skill_content

    @property
    def loaded_skills(self) -> List[str]:
        """Return list of skill names that have been loaded and consulted."""
        return list(self._loaded_skills.keys())

    def record_loaded_skill(self, name: str) -> None:
        """Explicitly record a skill as loaded and consulted."""
        if name not in self._loaded_skills:
            try:
                self.get_skill_content(name)
            except KeyError:
                meta = SkillMetadata(name=name, description="", path=self.skills_dir / name)
                self._loaded_skills[name] = SkillContent(
                    metadata=meta,
                    instructions="",
                    reference_contents={},
                )

    def reset_loaded_skills(self) -> None:
        """Reset the set of loaded skills."""
        self._loaded_skills.clear()

    def load_skills_mapping(
        self,
        mapping_file: Optional[Union[str, Path]] = None,
    ) -> Dict[str, List[str]]:
        """Load skills.yaml defining which skills belong to which specialists or dispatcher."""
        target_file = (
            Path(mapping_file).resolve()
            if mapping_file is not None
            else self.skills_dir / "skills.yaml"
        )
        if not target_file.exists():
            return {}

        raw_yaml = target_file.read_text(encoding="utf-8")
        data: Any = yaml.safe_load(raw_yaml)
        if not isinstance(data, dict):
            return {}

        # Normalize data: target -> list of skill names
        mapping: Dict[str, List[str]] = {}
        for target, skills in data.items():
            if isinstance(skills, list):
                mapping[str(target)] = [str(s) for s in skills]
            elif isinstance(skills, str):
                mapping[str(target)] = [skills]

        self._skills_mapping = mapping
        return self._skills_mapping

    def get_skills_for_target(
        self,
        target: str,
        service_name: Optional[str] = None,
    ) -> List[SkillMetadata]:
        """Return all skills applicable to a specific target (including dynamic service runbook)."""
        if not self._skills_mapping:
            self.load_skills_mapping()
        if not self._metadata_cache:
            self.discover_skills()

        target_skill_names = set(self._skills_mapping.get(target, []))
        all_skill_names = set(self._skills_mapping.get("all", []))
        applicable_names = target_skill_names | all_skill_names
        if service_name:
            clean_service = service_name.strip()
            runbook_name = (
                clean_service
                if clean_service.startswith("service-")
                else f"service-{clean_service}"
            )
            applicable_names.add(runbook_name)

        results: List[SkillMetadata] = []
        for name in applicable_names:
            meta = self._metadata_cache.get(name)
            if meta:
                results.append(meta)
        return sorted(results, key=lambda s: s.name)


def get_skill_toolset_for_target(
    target: str,
    service_name: Optional[str] = None,
    skills_dir: Optional[Union[str, Path]] = None,
    tool_filter: Optional[List[str]] = None,
) -> Any:
    """Create an ADK SkillToolset for a specific target agent using skills.yaml mapping.

    Dynamically resolves service runbook as 'service-<service_name>' at runtime
    when service_name is provided, without hardcoding service names into static maps
    or code (enforcing de-overfit guard).
    """
    from google.adk.skills import load_skills_from_dir
    from google.adk.tools.skill_toolset import SkillToolset

    if isinstance(service_name, Path):
        skills_dir = service_name
        service_name = None

    if skills_dir is None:
        base_path = Path(__file__).resolve().parent.parent / "skills"
    else:
        base_path = Path(skills_dir).resolve()

    mapping_file = base_path / "skills.yaml"
    mapping: Dict[str, List[str]] = {}
    if mapping_file.exists():
        raw_yaml = mapping_file.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_yaml) or {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    mapping[str(k)] = [str(item) for item in v]
                elif isinstance(v, str):
                    mapping[str(k)] = [v]

    target_skill_names = set(mapping.get(target, [])) | set(mapping.get("all", []))
    if service_name:
        clean_service = service_name.strip()
        runbook_name = (
            clean_service if clean_service.startswith("service-") else f"service-{clean_service}"
        )
        target_skill_names.add(runbook_name)

    all_skills = load_skills_from_dir(base_path)
    selected_skills = [s for s in all_skills if s.name in target_skill_names]

    effective_tool_filter = (
        tool_filter
        if tool_filter is not None
        else ["list_skills", "load_skill", "load_skill_resource"]
    )

    return SkillToolset(
        skills=selected_skills,
        tool_filter=effective_tool_filter,
    )
