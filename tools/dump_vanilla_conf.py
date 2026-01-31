#!/usr/bin/env python3

"""
Script for generating default wsmod config from a vanilla game's files
warning: bad
"""

from pathlib import Path
import struct
from collections import namedtuple
import logging
import sys
import json
from typing import Dict, List, Optional, Set, Tuple

VANILLA_ROOT_PATH = Path(
    "/mnt/c/Users/ComplexPlane/Documents/projects/romhack/smb2imm/files"
)

CourseCommand = namedtuple("CourseCommand", ["opcode", "type", "value"])
SmStageInfo = namedtuple("SmStageInfo", ["stage_id", "difficulty"])

# CMD opcodes
CMD_IF = 0
CMD_THEN = 1
CMD_FLOOR = 2
CMD_COURSE_END = 3

# CMD_IF conditions
IF_FLOOR_CLEAR = 0
IF_GOAL_TYPE = 2

# CMD_THEN actions
THEN_JUMP_FLOOR = 0
THEN_END_COURSE = 2

# CMD_FLOOR value types
FLOOR_STAGE_ID = 0
FLOOR_TIME = 1


def get_theme_and_music_ids(stage_id, stage_id_to_theme_id, theme_id_to_music_id):
    if stage_id < 0 or stage_id >= len(stage_id_to_theme_id):
        logging.warning("Stage id %s out of range for theme map; using theme 0", stage_id)
        theme_id = 0
    else:
        theme_id = stage_id_to_theme_id[stage_id]
    if theme_id > 42:
        theme_id = 42
    if theme_id < 0 or theme_id >= len(theme_id_to_music_id):
        logging.warning("Theme id %s out of range for music map; using 0", theme_id)
        music_id = 0
    else:
        music_id = theme_id_to_music_id[theme_id]
    return (theme_id, music_id)


def parse_cm_course(
    mainloop_buffer,
    stgname_lines,
    bonus_stage_ids,
    stage_id_to_theme_id_map,
    theme_id_to_music_id_map,
    start,
    count=None,
    max_cmds=1024,
    strict=True,
):
    def raise_error(message: str):
        logging.error(message)
        if strict:
            raise SystemExit(message)
        raise ValueError(message)

    cmds: list[CourseCommand] = []
    course_cmd_size = 0x1C

    if count is None:
        i = 0
        while start + (i + 1) * course_cmd_size <= len(mainloop_buffer) and i < max_cmds:
            course_cmd = CourseCommand._make(
                struct.unpack_from(
                    ">BBxxI20x",
                    mainloop_buffer,
                    start + i * course_cmd_size,
                )
            )
            cmds.append(course_cmd)
            if course_cmd.opcode == CMD_COURSE_END:
                break
            i += 1
    else:
        for i in range(count):
            course_cmd = CourseCommand._make(
                struct.unpack_from(
                    ">BBxxI20x",
                    mainloop_buffer,
                    start + i * course_cmd_size,
                )
            )
            cmds.append(course_cmd)

    # Course commands to stage infos
    cm_stage_infos = []
    stage_id = 0
    stage_time = 60 * 60
    blue_jump = None
    green_jump = None
    red_jump = None
    last_goal_type = None
    first = True
    finished = False

    for cmd in cmds:
        if cmd.opcode == CMD_FLOOR:
            if cmd.type == FLOOR_STAGE_ID:
                if not first:
                    if blue_jump is None:
                        raise_error("Invalid blue goal jump")

                    theme_id, music_id = get_theme_and_music_ids(
                        stage_id, stage_id_to_theme_id_map, theme_id_to_music_id_map
                    )

                    stage_name = (
                        stgname_lines[stage_id]
                        if 0 <= stage_id < len(stgname_lines)
                        else f"Stage {stage_id}"
                    )
                    cm_stage_infos.append(
                        {
                            "stage_id": stage_id,
                            "name": stage_name,
                            "theme_id": theme_id,
                            "music_id": music_id,
                            "time_limit": float(stage_time / 60),
                            "blue_goal_jump": blue_jump,
                            "green_goal_jump": green_jump
                            if green_jump is not None
                            else blue_jump,
                            "red_goal_jump": red_jump
                            if red_jump is not None
                            else blue_jump,
                            "is_bonus_stage": stage_id in bonus_stage_ids,
                        }
                    )
                    stage_id = 0
                    stage_time = 60 * 60
                    blue_jump = None
                    green_jump = None
                    red_jump = None
                    last_goal_type = None

                stage_id = cmd.value
                first = False

            elif cmd.type == FLOOR_TIME:
                stage_time = cmd.value
            else:
                raise_error(f"Invalid CMD_FLOOR opcode type: {cmd.type}")

        elif cmd.opcode == CMD_IF:
            if cmd.type == IF_FLOOR_CLEAR:
                last_goal_type = None
            elif cmd.type == IF_GOAL_TYPE:
                last_goal_type = cmd.value
            else:
                raise_error(f"Invalid CMD_IF opcode type: {cmd.type}")

        elif cmd.opcode == CMD_THEN:
            if cmd.type == THEN_JUMP_FLOOR:
                if last_goal_type is None:
                    if blue_jump is None:
                        blue_jump = cmd.value
                    if green_jump is None:
                        green_jump = cmd.value
                    if red_jump is None:
                        red_jump = cmd.value
                elif last_goal_type == 0:
                    blue_jump = cmd.value
                elif last_goal_type == 1:
                    green_jump = cmd.value
                elif last_goal_type == 2:
                    red_jump = cmd.value
                else:
                    raise_error(f"Invalid last goal type: {last_goal_type}")
            elif cmd.type == THEN_END_COURSE:
                # Jumps are irrelevant, this is end of difficulty
                blue_jump = 1
                green_jump = 1
                red_jump = 1
            else:
                raise_error(f"Invalid CMD_THEN opcode type: {cmd.type}")

        elif cmd.opcode == CMD_COURSE_END:
            if blue_jump is None:
                raise_error("Invalid blue goal jump")
            theme_id, music_id = get_theme_and_music_ids(
                stage_id, stage_id_to_theme_id_map, theme_id_to_music_id_map
            )
            stage_name = (
                stgname_lines[stage_id]
                if 0 <= stage_id < len(stgname_lines)
                else f"Stage {stage_id}"
            )
            cm_stage_infos.append(
                {
                    "stage_id": stage_id,
                    "name": stage_name,
                    "theme_id": theme_id,
                    "music_id": music_id,
                    "time_limit": float(stage_time / 60),
                    "blue_goal_jump": blue_jump,
                    "green_goal_jump": green_jump
                    if green_jump is not None
                    else blue_jump,
                    "red_goal_jump": red_jump if red_jump is not None else blue_jump,
                    "is_bonus_stage": stage_id in bonus_stage_ids,
                }
            )
            finished = True

        else:
            raise_error(f"Invalid opcode: {cmd.opcode}")

    if not finished:
        raise_error("Course command list ended early")

    return cm_stage_infos


def annotate_cm_layout_dump(dump: str) -> str:
    lines = dump.split("\n")
    out_lines: list[str] = []

    last_course = None
    floor_num = 1
    for line in lines:

        old_floor_num = floor_num
        floor_num = 1
        if '"beginner"' in line:
            last_course = "Beginner"
        elif '"beginner_extra"' in line:
            last_course = "Beginner Extra"
        elif '"advanced"' in line:
            last_course = "Advanced"
        elif '"advanced_extra"' in line:
            last_course = "Advanced Extra"
        elif '"expert"' in line:
            last_course = "Expert"
        elif '"expert_extra"' in line:
            last_course = "Expert Extra"
        elif '"master"' in line:
            last_course = "Master"
        elif '"master_extra"' in line:
            last_course = "Master Extra"
        else:
            # Don't reset floor num if new difficulty not detected
            floor_num = old_floor_num

        new_line = line[:]
        if "{" in new_line and last_course is not None:
            new_line += f" // {last_course} {floor_num}"
            floor_num += 1

        new_line = new_line.replace("60.0", "60.00")
        new_line = new_line.replace("30.0", "30.00")

        out_lines.append(new_line)

        # if '"time_limit"' in new_line:
        #     out_lines.append("")

    return "\n".join(out_lines)


def dump_storymode_world_layout(
    mainloop_buffer,
    stgname_lines,
    stage_id_to_theme_id_map,
    theme_id_to_music_id_map,
    start,
):
    stage_info_size = 0x4

    stage_infos: list[SmStageInfo] = []
    for i in range(10):
        offs = start + i * stage_info_size
        stage_info = SmStageInfo._make(struct.unpack_from(">hh", mainloop_buffer, offs))
        stage_infos.append(stage_info)

    out_json_array = []
    for stage_info in stage_infos:
        time_limit = 60 * 60 if stage_info.stage_id != 30 else 60 * 30
        theme_id, music_id = get_theme_and_music_ids(
            stage_info.stage_id, stage_id_to_theme_id_map, theme_id_to_music_id_map
        )
        stage_name = (
            stgname_lines[stage_info.stage_id]
            if 0 <= stage_info.stage_id < len(stgname_lines)
            else f"Stage {stage_info.stage_id}"
        )
        out_json_array.append(
            {
                "stage_id": stage_info.stage_id,
                "name": stage_name,
                "theme_id": theme_id,
                "music_id": music_id,
                "time_limit": float(time_limit / 60),
                "difficulty": stage_info.difficulty,
            }
        )

    return out_json_array


def annotate_story_layout_dump(dump: str) -> str:
    lines = dump.split("\n")
    out_lines: list[str] = []

    last_course = None
    world = -1
    stage = 0
    for line in lines:
        new_line = line[:]

        if "[" in line:
            world += 1
            stage = 0
            if world >= 1:
                new_line += f" // World {world}"
        if "{" in line:
            stage += 1
            new_line += f" // Stage {world}-{stage}"

        new_line = new_line.replace("60.0", "60.00")
        new_line = new_line.replace("30.0", "30.00")

        out_lines.append(new_line)

        # if '"time_limit"' in new_line:
        #     out_lines.append("")

    return "\n".join(out_lines)


def list_stage_ids(stage_dir: Path) -> Set[int]:
    ids: Set[int] = set()
    if not stage_dir.exists():
        return ids
    for path in stage_dir.glob("STAGE*.lz"):
        name = path.name
        if len(name) == 11 and name.startswith("STAGE") and name.endswith(".lz"):
            try:
                ids.add(int(name[5:8]))
            except ValueError:
                continue
    return ids


def collect_stage_ids_from_cm(cm_layout: Dict[str, List[dict]]) -> List[int]:
    ids: List[int] = []
    for entries in cm_layout.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("stage_id"), int):
                ids.append(entry["stage_id"])
    return ids


def collect_stage_ids_from_story(worlds: List[List[dict]]) -> List[int]:
    ids: List[int] = []
    for world in worlds:
        if not isinstance(world, list):
            continue
        for entry in world:
            if isinstance(entry, dict) and isinstance(entry.get("stage_id"), int):
                ids.append(entry["stage_id"])
    return ids


def validate_stage_ids(
    stage_ids: List[int],
    valid_ids: Set[int],
    label: str,
    named_ids: Optional[Set[int]] = None,
    min_named_ratio: float = 0.0,
) -> bool:
    if not stage_ids or not valid_ids:
        return True
    if any(stage_id < 0 for stage_id in stage_ids):
        logging.warning("%s contains negative stage ids", label)
        return False
    invalid = [sid for sid in stage_ids if sid not in valid_ids]
    if not invalid:
        if named_ids and min_named_ratio > 0:
            named_count = sum(1 for sid in stage_ids if sid in named_ids)
            ratio = named_count / max(1, len(stage_ids))
            if ratio < min_named_ratio:
                logging.warning("%s has low named stage ratio (%.1f%%)", label, ratio * 100)
                return False
        return True
    ratio = len(invalid) / max(1, len(stage_ids))
    logging.warning("%s has %d invalid stage ids (%.1f%%)", label, len(invalid), ratio * 100)
    return ratio < 0.1


def find_course_offsets(
    data: bytes,
    stage_ids: Set[int],
    named_stage_ids: Set[int],
    min_stages: int = 10,
    max_cmds: int = 512,
) -> List[Tuple[int, int]]:
    course_cmd_size = 0x1C
    candidates: List[Tuple[int, int, float]] = []
    for off in range(0, len(data) - course_cmd_size, 4):
        opcode = data[off]
        cmd_type = data[off + 1]
        if opcode != CMD_FLOOR or cmd_type != FLOOR_STAGE_ID:
            continue
        stage_count = 0
        valid_stage_count = 0
        cmd_count = 0
        finished = False
        for i in range(max_cmds):
            cmd_off = off + i * course_cmd_size
            if cmd_off + course_cmd_size > len(data):
                break
            opcode = data[cmd_off]
            cmd_type = data[cmd_off + 1]
            value = struct.unpack_from(">I", data, cmd_off + 4)[0]
            cmd_count += 1
            if opcode == CMD_FLOOR:
                if cmd_type == FLOOR_STAGE_ID:
                    stage_count += 1
                    if value in stage_ids:
                        valid_stage_count += 1
                elif cmd_type != FLOOR_TIME:
                    break
            elif opcode == CMD_IF:
                if cmd_type not in (IF_FLOOR_CLEAR, IF_GOAL_TYPE):
                    break
            elif opcode == CMD_THEN:
                if cmd_type not in (THEN_JUMP_FLOOR, THEN_END_COURSE):
                    break
            elif opcode == CMD_COURSE_END:
                finished = True
                break
            else:
                break
        if not finished or stage_count < min_stages:
            continue
        ratio = valid_stage_count / max(1, stage_count)
        named_count = 0
        if named_stage_ids:
            for i in range(max_cmds):
                cmd_off = off + i * course_cmd_size
                if cmd_off + course_cmd_size > len(data):
                    break
                opcode = data[cmd_off]
                cmd_type = data[cmd_off + 1]
                if opcode == CMD_FLOOR and cmd_type == FLOOR_STAGE_ID:
                    value = struct.unpack_from(">I", data, cmd_off + 4)[0]
                    if value in named_stage_ids:
                        named_count += 1
            named_ratio = named_count / max(1, stage_count)
        else:
            named_ratio = 0.0
        score = stage_count * ratio - cmd_count * 0.05 + named_ratio
        candidates.append((off, cmd_count, score))

    candidates.sort(key=lambda item: (-item[2], item[0]))
    selected: List[Tuple[int, int]] = []
    used_ranges: List[Tuple[int, int]] = []
    for off, cmd_count, _ in candidates:
        start = off
        end = off + cmd_count * course_cmd_size
        if any(start < rng_end and end > rng_start for rng_start, rng_end in used_ranges):
            continue
        selected.append((off, cmd_count))
        used_ranges.append((start, end))
        if len(selected) >= 8:
            break
    selected.sort(key=lambda item: item[0])
    return selected


def find_story_block_offset(
    data: bytes,
    stage_ids: Set[int],
    named_stage_ids: Set[int],
) -> Optional[int]:
    entry_size = 4
    world_count = 10
    stages_per_world = 10
    block_size = world_count * stages_per_world * entry_size
    for off in range(0, len(data) - block_size, 4):
        valid = True
        unique_ids: Set[int] = set()
        named_count = 0
        for idx in range(world_count * stages_per_world):
            entry_off = off + idx * entry_size
            stage_id, difficulty = struct.unpack_from(">hh", data, entry_off)
            if stage_id not in stage_ids:
                valid = False
                break
            if difficulty < 0 or difficulty > 5:
                valid = False
                break
            unique_ids.add(stage_id)
            if stage_id in named_stage_ids:
                named_count += 1
        if valid:
            if len(unique_ids) < 20:
                continue
            if named_stage_ids and named_count / max(1, world_count * stages_per_world) < 0.3:
                continue
            return off
    return None


def is_story_world_valid(
    data: bytes,
    offset: int,
    stage_ids: Set[int],
    named_stage_ids: Set[int],
) -> bool:
    unique_ids: Set[int] = set()
    named_count = 0
    for idx in range(10):
        stage_id, difficulty = struct.unpack_from(">hh", data, offset + idx * 4)
        if stage_id not in stage_ids:
            return False
        if difficulty < 0 or difficulty > 5:
            return False
        unique_ids.add(stage_id)
        if stage_id in named_stage_ids:
            named_count += 1
    if len(unique_ids) < 3:
        return False
    if named_stage_ids and named_count / 10 < 0.3:
        return False
    return True


def load_vanilla_course_data(
    rom_dir: Path,
    *,
    course_cmd_counts: Optional[Dict[str, int]] = None,
    world_offsets: Optional[List[int]] = None,
) -> dict:
    mainloop_path = rom_dir / "mkb2.main_loop.rel"
    stgname_path = rom_dir / "stgname" / "usa.str"
    if not mainloop_path.exists():
        raise FileNotFoundError(f"missing {mainloop_path}")
    if not stgname_path.exists():
        raise FileNotFoundError(f"missing {stgname_path}")

    mainloop_buffer = mainloop_path.read_bytes()
    stgname_lines = stgname_path.read_text(encoding="ascii", errors="ignore").splitlines()
    named_stage_ids = {i for i, name in enumerate(stgname_lines) if name and name != "-"}

    bonus_stage_ids = struct.unpack_from(">9i", mainloop_buffer, 0x00176118)
    stage_id_to_theme_id_map = struct.unpack_from(">428B", mainloop_buffer, 0x00204E48)
    theme_id_to_music_id_map = struct.unpack_from(">43h", mainloop_buffer, 0x0016E738)

    stage_ids = list_stage_ids(rom_dir / "stage")

    # Parse challenge mode entries using default offsets first.
    counts = course_cmd_counts or {}
    default_course_offsets = [
        ("beginner", 0x002075B0),
        ("advanced", 0x00207914),
        ("expert", 0x00208634),
        ("beginner_extra", 0x00209CF4),
        ("advanced_extra", 0x0020A0C8),
        ("expert_extra", 0x0020A448),
        ("master", 0x0020A8E0),
        ("master_extra", 0x0020ACB4),
    ]
    cm_layout: Dict[str, List[dict]] = {}
    for name, offset in default_course_offsets:
        try:
            cm_layout[name] = parse_cm_course(
                mainloop_buffer,
                stgname_lines,
                bonus_stage_ids,
                stage_id_to_theme_id_map,
                theme_id_to_music_id_map,
                offset,
                counts.get(name),
                strict=True,
            )
        except Exception:
            cm_layout = {}
            break

    if cm_layout:
        cm_ids = collect_stage_ids_from_cm(cm_layout)
        if not validate_stage_ids(cm_ids, stage_ids, "challenge courses", named_ids=named_stage_ids):
            cm_layout = {}

    if not cm_layout and stage_ids:
        logging.warning("Default course offsets invalid; scanning for course tables.")
        offsets = find_course_offsets(mainloop_buffer, stage_ids, named_stage_ids)
        order = [name for name, _ in default_course_offsets]
        for idx, (offset, cmd_count) in enumerate(offsets[: len(order)]):
            name = order[idx]
            try:
                cm_layout[name] = parse_cm_course(
                    mainloop_buffer,
                    stgname_lines,
                    bonus_stage_ids,
                    stage_id_to_theme_id_map,
                    theme_id_to_music_id_map,
                    offset,
                    cmd_count,
                    strict=False,
                )
            except Exception:
                cm_layout = {}
                break

    if not cm_layout:
        raise SystemExit("Failed to locate challenge course tables.")

    if world_offsets is None:
        world_offsets = [
            0x0020b448,
            0x0020b470,
            0x0020b498,
            0x0020b4c0,
            0x0020b4e8,
            0x0020b510,
            0x0020b538,
            0x0020b560,
            0x0020b588,
            0x0020b5b0,
        ]
    worlds = []
    for offs in world_offsets:
        if stage_ids and not is_story_world_valid(mainloop_buffer, offs, stage_ids, named_stage_ids):
            worlds = []
            break
        world = dump_storymode_world_layout(
            mainloop_buffer,
            stgname_lines,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            offs,
        )
        worlds.append(world)

    if worlds:
        story_ids = collect_stage_ids_from_story(worlds)
        if not validate_stage_ids(
            story_ids,
            stage_ids,
            "story worlds",
            named_ids=named_stage_ids,
            min_named_ratio=0.3,
        ):
            worlds = []

    if not worlds and stage_ids:
        logging.warning("Default story offsets invalid; scanning for story table.")
        base_off = find_story_block_offset(mainloop_buffer, stage_ids, named_stage_ids)
        if base_off is not None:
            world_offsets = [base_off + i * 0x28 for i in range(10)]
            for offs in world_offsets:
                world = dump_storymode_world_layout(
                    mainloop_buffer,
                    stgname_lines,
                    stage_id_to_theme_id_map,
                    theme_id_to_music_id_map,
                    offs,
                )
                worlds.append(world)

    if not worlds:
        logging.warning("Story world data not found; output will omit story worlds.")

    return {
        "challenge": cm_layout,
        "story": worlds,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Dump vanilla challenge/story course data from extracted SMB2 files."
    )
    parser.add_argument(
        "--rom",
        type=Path,
        default=VANILLA_ROOT_PATH,
        help="Path to extracted ROM folder (containing mkb2.main_loop.rel)",
    )
    args = parser.parse_args()

    data = load_vanilla_course_data(args.rom)
    cm_layout_dump = json.dumps(data["challenge"], indent=4)
    annotated_cm_layout_dump = annotate_cm_layout_dump(cm_layout_dump)
    print(annotated_cm_layout_dump)

    story_layout_dump = json.dumps(data["story"], indent=4)
    annotated_story_layout_dump = annotate_story_layout_dump(story_layout_dump)
    # print(annotated_story_layout_dump)


if __name__ == "__main__":
    main()
