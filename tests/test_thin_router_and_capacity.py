import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SKILL = ROOT / "SKILL.md"
REF = ROOT / "references"


def load_capacity_module():
    path = ROOT / "scripts" / "estimate_capacity.py"
    spec = importlib.util.spec_from_file_location("estimate_capacity", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_script_module(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class ThinRouterTests(unittest.TestCase):
    def test_skill_identity_matches_folder_and_ui(self):
        text = SKILL.read_text(encoding="utf-8")
        ui = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertEqual(ROOT.name, "diagonal-director-master")
        self.assertIn("name: diagonal-director-master", text)
        self.assertIn('display_name: "DIAGONAL导演大师"', ui)
        self.assertIn("$diagonal-director-master", ui)

    def test_main_skill_is_thin(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 150)
        self.assertNotIn("## 第0步：建立全片基线", text)
        self.assertNotIn("## 单场固定五步流程", text)
        self.assertIn("## 入口路由", text)
        self.assertIn("Duration / Capacity", text)

    def test_router_targets_exist(self):
        text = SKILL.read_text(encoding="utf-8")
        expected = {
            "storyboard-project-workflow.md",
            "storyboard-scene-workflow.md",
            "story-time-continuity-system.md",
            "duration-capacity-system.md",
            "lighting-system.md",
            "acting-director-system.md",
            "costume-system.md",
            "makeup-hair-system.md",
            "vision-prompt.md",
            "midjourney-location-prompt-system.md",
            "seedance-prompt-system.md",
            "seedance-optics-system.md",
        }
        for name in expected:
            self.assertIn(f"references/{name}", text)
            self.assertTrue((REF / name).is_file(), name)

    def test_project_rules_were_moved_not_dropped(self):
        text = (REF / "storyboard-project-workflow.md").read_text(encoding="utf-8")
        for marker in (
            "## 第0步：建立全片基线",
            "### 全片情绪曲线",
            "### 连续性总系统",
            "### 全片光影圣经",
            "### 场景图视觉分析（可选）",
            "### Midjourney 场地图提示词（按需）",
            "### Seedance 2.0 视频提示词（按需）",
            "### 开始前确认",
        ):
            self.assertIn(marker, text)

    def test_scene_rules_were_moved_not_dropped(self):
        text = (REF / "storyboard-scene-workflow.md").read_text(encoding="utf-8")
        for marker in (
            "### ① 分析本场重点",
            "### ② 分镜简稿",
            "### ③ 完整分镜预览与自检（仅对话展示）",
            "### ④ 必要的人物可见状态细化",
            "### ⑤ 最终复检与定稿写入",
            "## 全片完成后",
            "### 跨场一致性检查",
            "### 合并与统计",
        ):
            self.assertIn(marker, text)

    def test_reference_links_are_valid_and_acyclic(self):
        markdown_files = [SKILL, *sorted(REF.rglob("*.md"))]
        graph = {path.resolve(): set() for path in markdown_files}
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")
        for path in markdown_files:
            for raw_target in link_pattern.findall(path.read_text(encoding="utf-8")):
                if "://" in raw_target:
                    continue
                target = (path.parent / raw_target).resolve()
                self.assertTrue(target.is_file(), f"broken link: {path} -> {raw_target}")
                if target in graph:
                    graph[path.resolve()].add(target)

        visiting = set()
        visited = set()

        def visit(node):
            self.assertNotIn(node, visiting, f"reference cycle detected at {node}")
            if node in visited:
                return
            visiting.add(node)
            for target in graph[node]:
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)

    def test_supporting_modules_do_not_use_root_relative_literal_paths(self):
        forbidden = re.compile(r"`(?:references/|scripts/|SKILL\.md`)")
        for path in REF.rglob("*.md"):
            self.assertIsNone(forbidden.search(path.read_text(encoding="utf-8")), path.name)

    def test_current_installed_bugfixes_are_preserved(self):
        project = (REF / "storyboard-project-workflow.md").read_text(encoding="utf-8")
        scene = (REF / "storyboard-scene-workflow.md").read_text(encoding="utf-8")
        main = SKILL.read_text(encoding="utf-8")
        self.assertIn("只要 `storyboard_locations.md` 存在就读取", project)
        self.assertIn("不以是否使用 Midjourney 为前提", project)
        self.assertIn("先从 `storyboard_continuity.md` 只读取当前场需要的信息", scene)
        self.assertIn("场次起始（继承本场开始状态）", scene)
        self.assertIn("存在经用户确认锁定的正式空间或场地事实时维护", main)

    def test_all_original_workflow_headings_are_preserved(self):
        old_headings = {
            "入口路由", "总流程", "规则优先级", "第0步：建立全片基线",
            "场次、人物和关键物品", "全片情绪曲线", "总体走向", "逐场曲线",
            "曲线诊断", "情绪曲线图片", "连续性总系统", "剧情时间表",
            "全片光影圣经", "场景图视觉分析（可选）", "Midjourney 场地图提示词（按需）",
            "Seedance 2.0 视频提示词（按需）", "开始前确认", "单场固定五步流程",
            "① 分析本场重点", "第X场 场景灯光卡", "第X场人物状态与连续性确认",
            "剧情时间", "开始状态", "预计变化", "关键物品与固定光源", "待确认",
            "② 分镜简稿", "③ 完整分镜预览与自检（仅对话展示）",
            "镜号：X-Y 时长：Z秒 景别：XXX 运镜：XXX 角度：XXX 主体：XXX",
            "对白规则", "镜头结构修改边界", "自检项目", "镜头时间与状态继承",
            "对账报告", "镜号 X-Y 对账", "④ 必要的人物可见状态细化",
            "⑤ 最终复检与定稿写入", "第X场结束状态", "全片完成后",
            "跨场一致性检查", "合并与统计", "行为底线",
        }
        routed_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, REF / "storyboard-project-workflow.md", REF / "storyboard-scene-workflow.md")
        )
        new_headings = set(re.findall(r"^#{2,6} (.+)$", routed_text, re.M))
        self.assertFalse(old_headings - new_headings, f"missing headings: {sorted(old_headings - new_headings)}")


class CapacityContractTests(unittest.TestCase):
    def test_no_hardcoded_default_generation_limit(self):
        text = (REF / "duration-capacity-system.md").read_text(encoding="utf-8")
        self.assertIn("未给出时不得自行假设 10 秒、15 秒、30 秒", text)
        self.assertIn("银幕时长不是生成段上限", text)

    def test_capacity_is_integrated_into_scene_workflow(self):
        text = (REF / "storyboard-scene-workflow.md").read_text(encoding="utf-8")
        self.assertIn("完成简稿后必须执行容量预检", text)
        self.assertIn("14. **时长与容量**", text)
        self.assertIn("最终时长与容量复核", text)

    def test_capacity_is_integrated_into_seedance(self):
        text = (REF / "seedance-prompt-system.md").read_text(encoding="utf-8")
        self.assertIn("duration-capacity-system.md", text)
        self.assertIn("生成段容量冲突", text)

    def test_capacity_has_no_rewrite_or_auto_split_authority(self):
        capacity = (REF / "duration-capacity-system.md").read_text(encoding="utf-8")
        scene = (REF / "storyboard-scene-workflow.md").read_text(encoding="utf-8")
        seedance = (REF / "seedance-prompt-system.md").read_text(encoding="utf-8")
        self.assertIn("不授予改写剧本、对白或镜头结构的权限", capacity)
        self.assertIn("不擅自拆镜", capacity)
        self.assertIn("必须返回第②步", scene)
        self.assertIn("不自动拆改正式分镜", seedance)

    def test_no_foreign_adapter_preferences_or_platform_defaults(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in [SKILL, *REF.rglob("*.md")])
        self.assertNotRegex(text, re.compile(r"Mr[ ._-]*Li|李老师", re.I))
        self.assertNotRegex(text, r"默认\s*(?:10|15|30)\s*秒")


class CapacityEstimatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_capacity_module()

    def test_dialogue_calibration(self):
        result = self.mod.estimate({"dialogue_chars": 42})
        self.assertAlmostEqual(result.dialogue_min_seconds, 10.0, places=2)
        self.assertAlmostEqual(result.dialogue_max_seconds, 12.0, places=2)
        self.assertEqual(result.status, "不确定")

    def test_overload_detection(self):
        result = self.mod.estimate({
            "dialogue_chars": 42,
            "serial_action_seconds": 5,
            "pause_seconds": 2,
            "declared_seconds": 12,
        })
        self.assertEqual(result.status, "过载")
        self.assertGreater(result.natural_min_seconds, 12)

    def test_confirmed_overlap_only_reduces_non_dialogue(self):
        result = self.mod.estimate({
            "dialogue_chars": 0,
            "movement_seconds": 3,
            "reaction_seconds": 2,
            "confirmed_overlap_seconds": 99,
        })
        self.assertEqual(result.confirmed_overlap_seconds, 2.0)
        self.assertEqual(result.longest_non_dialogue_component_seconds, 3.0)
        self.assertEqual(result.natural_min_seconds, 3.0)

    def test_action_and_dialogue_overlap_are_separate(self):
        result = self.mod.estimate({
            "dialogue_chars": 42,
            "movement_seconds": 3,
            "reaction_seconds": 2,
            "action_action_overlap_seconds": 2,
            "dialogue_action_overlap_seconds": 3,
        })
        self.assertEqual(result.action_action_overlap_seconds, 2.0)
        self.assertEqual(result.dialogue_action_overlap_min_seconds, 3.0)
        self.assertEqual(result.natural_min_seconds, 10.0)
        self.assertEqual(result.natural_max_seconds, 12.0)

    def test_legacy_and_new_action_overlap_cannot_be_mixed(self):
        with self.assertRaises(ValueError):
            self.mod.estimate({
                "movement_seconds": 2,
                "confirmed_overlap_seconds": 1,
                "action_action_overlap_seconds": 1,
            })

    def test_invalid_negative_input_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.estimate({"dialogue_chars": -1})

    def test_non_finite_input_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.estimate({"pause_seconds": "nan"})


class CapacityBlackBoxTests(unittest.TestCase):
    SCRIPT = ROOT / "scripts" / "estimate_capacity.py"

    def run_cli(self, payload):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "case.json"
            input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(self.SCRIPT), str(input_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        return json.loads(completed.stdout)

    def test_dialogue_and_action_overload(self):
        result = self.run_cli({
            "dialogue_chars": 42,
            "serial_action_seconds": 5,
            "pause_seconds": 2,
            "declared_seconds": 12,
        })
        self.assertEqual(result["status"], "过载")
        self.assertGreater(result["natural_min_seconds"], 12)

    def test_normal_capacity(self):
        result = self.run_cli({
            "dialogue_chars": 20,
            "serial_action_seconds": 2,
            "declared_seconds": 10,
        })
        self.assertEqual(result["status"], "充裕")
        self.assertEqual(result["generation_status"], "未指定生成段上限")

    def test_action_only_shot(self):
        result = self.run_cli({
            "dialogue_chars": 0,
            "movement_seconds": 4,
            "reaction_seconds": 2,
            "declared_seconds": 6,
        })
        self.assertEqual(result["dialogue_max_seconds"], 0)
        self.assertEqual(result["natural_max_seconds"], 6)
        self.assertEqual(result["status"], "充裕")

    def test_generation_limit_conflict_is_separate(self):
        result = self.run_cli({
            "dialogue_chars": 0,
            "serial_action_seconds": 18,
            "declared_seconds": 18,
            "generation_limit_seconds": 12,
        })
        self.assertEqual(result["status"], "充裕")
        self.assertEqual(result["generation_status"], "冲突")
        self.assertEqual(result["generation_excess_seconds"], 6)
        self.assertEqual(result["generation_natural_risk"], "确定冲突")

    def test_invalid_screen_duration_blocks_generation_status(self):
        result = self.run_cli({
            "dialogue_chars": 42,
            "serial_action_seconds": 5,
            "pause_seconds": 2,
            "declared_seconds": 12,
            "generation_limit_seconds": 15,
        })
        self.assertEqual(result["status"], "过载")
        self.assertEqual(result["generation_status"], "待修正银幕时长")
        self.assertIsNone(result["generation_excess_seconds"])
        self.assertEqual(result["generation_natural_risk"], "确定冲突")

    def test_generation_natural_range_can_report_possible_conflict(self):
        result = self.run_cli({
            "dialogue_chars": 20,
            "movement_seconds": 1,
            "declared_seconds": 6,
            "generation_limit_seconds": 6,
        })
        self.assertEqual(result["status"], "可行但偏紧")
        self.assertEqual(result["generation_status"], "未超限")
        self.assertEqual(result["generation_natural_risk"], "可能冲突")


class SafeFilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_script_module("safe_names")

    def test_chinese_is_preserved_and_path_syntax_removed(self):
        value = self.mod.sanitize_filename('../客厅/夜景:*?"<>|.md')
        self.assertIn("客厅", value)
        self.assertNotRegex(value, r'[/\\:*?"<>|]')
        self.assertNotIn("..", value)
        self.assertTrue(value.endswith(".md"))

    def test_collision_gets_stable_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "客厅.md").write_text("old", encoding="utf-8")
            self.assertEqual(self.mod.unique_path(directory, "客厅.md").name, "客厅-02.md")

    def test_windows_device_names_are_rejected_even_with_extensions(self):
        from asset_server import name_component
        self.assertEqual(self.mod.sanitize_component('CON.txt'), 'CON-file.txt')
        self.assertEqual(self.mod.sanitize_filename('LPT1.md'), 'LPT1-file.md')
        with self.assertRaises(ValueError):
            name_component('CON.txt')


class ProjectTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_script_module("safe_names")
        cls.mod = load_script_module("project_transaction")

    def test_multi_file_commit_and_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            source = root / "source"
            project.mkdir()
            source.mkdir()
            (project / "a.md").write_text("old-a", encoding="utf-8")
            (source / "a.md").write_text("new-a", encoding="utf-8")
            (source / "b.md").write_text("new-b", encoding="utf-8")
            state = self.mod.apply_transaction(project, [
                {"target": "a.md", "source": str(source / "a.md"), "replace_existing": True},
                {"target": "b.md", "source": str(source / "b.md")},
            ])
            self.assertIsNone(state["active_transaction"])
            self.assertEqual((project / "a.md").read_text(encoding="utf-8"), "new-a")
            self.assertEqual((project / "b.md").read_text(encoding="utf-8"), "new-b")
            backup = project / state["last_transaction"]["entries"][0]["backup"]
            self.assertEqual(backup.read_text(encoding="utf-8"), "old-a")

    def test_unfinished_transaction_rolls_forward(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            source = root / "source"
            project.mkdir()
            source.mkdir()
            (project / "a.md").write_text("old-a", encoding="utf-8")
            (project / "b.md").write_text("old-b", encoding="utf-8")
            (source / "a.md").write_text("new-a", encoding="utf-8")
            (source / "b.md").write_text("new-b", encoding="utf-8")
            txid = "interrupted-test"
            entries = self.mod._prepare_entries(project, [
                {"target": "a.md", "source": str(source / "a.md"), "replace_existing": True},
                {"target": "b.md", "source": str(source / "b.md"), "replace_existing": True},
            ], txid)
            state = self.mod._load_state(project)
            state["active_transaction"] = {"id": txid, "status": "prepared", "entries": entries}
            self.mod._atomic_json(project / self.mod.STATE_NAME, state)
            first = entries[0]
            Path(project / first["staged"]).replace(project / first["target"])

            recovered = self.mod.recover_transaction(project, "auto")
            self.assertIsNone(recovered["active_transaction"])
            self.assertEqual((project / "a.md").read_text(encoding="utf-8"), "new-a")
            self.assertEqual((project / "b.md").read_text(encoding="utf-8"), "new-b")

    def test_recovery_refuses_to_overwrite_unknown_manual_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            source = root / "source"
            project.mkdir()
            source.mkdir()
            (project / "a.md").write_text("old-a", encoding="utf-8")
            (source / "a.md").write_text("new-a", encoding="utf-8")
            txid = "manual-change-test"
            entries = self.mod._prepare_entries(project, [
                {"target": "a.md", "source": str(source / "a.md"), "replace_existing": True},
            ], txid)
            state = self.mod._load_state(project)
            state["active_transaction"] = {"id": txid, "status": "prepared", "entries": entries}
            self.mod._atomic_json(project / self.mod.STATE_NAME, state)
            (project / "a.md").write_text("user-edited-after-interruption", encoding="utf-8")
            Path(project / entries[0]["staged"]).unlink()

            with self.assertRaisesRegex(RuntimeError, "user-modified"):
                self.mod.recover_transaction(project, "auto")
            self.assertEqual(
                (project / "a.md").read_text(encoding="utf-8"),
                "user-edited-after-interruption",
            )

    def test_auto_recovery_rolls_back_when_staging_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            source = root / "source"
            project.mkdir()
            source.mkdir()
            for name in ("a.md", "b.md"):
                (project / name).write_text(f"old-{name}", encoding="utf-8")
                (source / name).write_text(f"new-{name}", encoding="utf-8")
            txid = "rollback-test"
            entries = self.mod._prepare_entries(project, [
                {"target": "a.md", "source": str(source / "a.md"), "replace_existing": True},
                {"target": "b.md", "source": str(source / "b.md"), "replace_existing": True},
            ], txid)
            state = self.mod._load_state(project)
            state["active_transaction"] = {"id": txid, "status": "prepared", "entries": entries}
            self.mod._atomic_json(project / self.mod.STATE_NAME, state)
            Path(project / entries[0]["staged"]).replace(project / entries[0]["target"])
            Path(project / entries[1]["staged"]).unlink()

            recovered = self.mod.recover_transaction(project, "auto")
            self.assertEqual(recovered["last_transaction"]["status"], "rolled_back")
            self.assertEqual((project / "a.md").read_text(encoding="utf-8"), "old-a.md")
            self.assertEqual((project / "b.md").read_text(encoding="utf-8"), "old-b.md")

    def test_cli_applies_transaction_spec(self):
        script = ROOT / "scripts" / "project_transaction.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            source = root / "scene.md"
            source.write_text("final scene", encoding="utf-8")
            spec = root / "transaction.json"
            spec.write_text(json.dumps({"writes": [{
                "target": "storyboard_第1场_客厅.md",
                "source": str(source),
            }]}, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(script), "apply", "--project-dir", str(project), "--spec", str(spec)],
                check=True,
                capture_output=True,
                text=True,
            )
            state = json.loads(completed.stdout)
            self.assertIsNone(state["active_transaction"])
            self.assertEqual(
                (project / "storyboard_第1场_客厅.md").read_text(encoding="utf-8"),
                "final scene",
            )


class SharedContractTests(unittest.TestCase):
    def test_mj_only_has_stable_output_contract(self):
        text = (REF / "midjourney-location-prompt-system.md").read_text(encoding="utf-8")
        self.assertIn("storyboard-mj/", text)
        self.assertIn("当前稳定工作目录", text)
        self.assertIn("safe_names.py", text)

    def test_authority_modules_defer_to_main_priority(self):
        main = SKILL.read_text(encoding="utf-8")
        continuity = (REF / "story-time-continuity-system.md").read_text(encoding="utf-8")
        lighting = (REF / "lighting-system.md").read_text(encoding="utf-8")
        self.assertIn("当前版本剧本的明确事实", main)
        self.assertIn("当前轮已经确认的正式分镜事实", main)
        self.assertIn("统一规则优先级", continuity)
        self.assertIn("统一规则优先级", lighting)

    def test_project_transaction_contract_is_integrated(self):
        project = (REF / "storyboard-project-workflow.md").read_text(encoding="utf-8")
        scene = (REF / "storyboard-scene-workflow.md").read_text(encoding="utf-8")
        self.assertIn(".storyboard_project_state.json", project)
        self.assertIn("project_transaction.py", project)
        self.assertIn("一次提交", scene)


if __name__ == "__main__":
    unittest.main()
