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
import argparse

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
    count,
):
    cmds: list[CourseCommand] = []
    course_cmd_size = 0x1C

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
                        logging.error("Invalid blue goal jump")
                        sys.exit(1)

                    theme_id, music_id = get_theme_and_music_ids(
                        stage_id, stage_id_to_theme_id_map, theme_id_to_music_id_map
                    )

                    cm_stage_infos.append(
                        {
                            "stage_id": stage_id,
                            "name": stgname_lines[stage_id],
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
                logging.error(f"Invalid CMD_FLOOR opcode type: {cmd.type}")
                sys.exit(1)

        elif cmd.opcode == CMD_IF:
            if cmd.type == IF_FLOOR_CLEAR:
                last_goal_type = None
            elif cmd.type == IF_GOAL_TYPE:
                last_goal_type = cmd.value
            else:
                logging.error(f"Invalid CMD_IF opcode type: {cmd.type}")
                sys.exit(1)

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
                    logging.error(f"Invalid last goal type: {last_goal_type}")
                    sys.exit(1)
            elif cmd.type == THEN_END_COURSE:
                # Jumps are irrelevant, this is end of difficulty
                blue_jump = 1
                green_jump = 1
                red_jump = 1
            else:
                logging.error(f"Invalid CMD_THEN opcode type: {cmd.type}")
                sys.exit(1)

        elif cmd.opcode == CMD_COURSE_END:
            if blue_jump is None:
                logging.error("Invalid blue goal jump")
                sys.exit(1)
            theme_id, music_id = get_theme_and_music_ids(
                stage_id, stage_id_to_theme_id_map, theme_id_to_music_id_map
            )
            cm_stage_infos.append(
                {
                    "stage_id": stage_id,
                    "name": stgname_lines[stage_id],
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
            logging.error(f"Invalid opcode: {cmd.opcode}")
            sys.exit(1)

    if not finished:
        logging.error("Course command list ended early")
        sys.exit(1)

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
        out_json_array.append(
            {
                "stage_id": stage_info.stage_id,
                "name": stgname_lines[stage_info.stage_id],
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


def list_stage_ids(stage_dir: Path) -> set[int]:
    ids: set[int] = set()
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


def validate_story_offsets(mainloop_buffer: bytes, world_offsets: list[int], stage_ids: set[int]) -> bool:
    if not stage_ids:
        return True
    for offs in world_offsets:
        for i in range(10):
            entry_offs = offs + i * 4
            if entry_offs + 4 > len(mainloop_buffer):
                return False
            stage_id, difficulty = struct.unpack_from(">hh", mainloop_buffer, entry_offs)
            if stage_id not in stage_ids:
                return False
            if difficulty < 0 or difficulty > 5:
                return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Dump vanilla challenge/story course data from extracted SMB2 files."
    )
    parser.add_argument(
        "--rom",
        type=Path,
        default=VANILLA_ROOT_PATH,
        help="Path to extracted ROM folder (containing mkb2.main_loop.rel)",
    )
    parser.add_argument(
        "--story-only",
        action="store_true",
        help="Skip challenge mode parsing and only dump story worlds.",
    )
    args = parser.parse_args()
    root_path = args.rom

    with open(root_path / "mkb2.main_loop.rel", "rb") as f:
        mainloop_buffer = f.read()
    with open(root_path / "stgname" / "usa.str", "r") as f:
        stgname_lines = [s.strip() for s in f.readlines()]

    bonus_stage_ids = struct.unpack_from(">9i", mainloop_buffer, 0x00176118)
    stage_id_to_theme_id_map = struct.unpack_from(">428B", mainloop_buffer, 0x00204E48)
    theme_id_to_music_id_map = struct.unpack_from(">43h", mainloop_buffer, 0x0016E738)

    if not args.story_only:
        # Parse challenge mode entries
        beginner = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x002075B0,
            31,
        )
        advanced = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x00207914,
            120,
        )
        expert = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x00208634,
            208,
        )
        beginner_extra = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x00209CF4,
            35,
        )
        advanced_extra = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x0020A0C8,
            32,
        )
        expert_extra = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x0020A448,
            42,
        )
        master = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x0020A8E0,
            35,
        )
        master_extra = parse_cm_course(
            mainloop_buffer,
            stgname_lines,
            bonus_stage_ids,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            0x0020ACB4,
            50,
        )
        cm_layout = {
            "beginner": beginner,
            "beginner_extra": beginner_extra,
            "advanced": advanced,
            "advanced_extra": advanced_extra,
            "expert": expert,
            "expert_extra": expert_extra,
            "master": master,
            "master_extra": master_extra,
        }

        cm_layout_dump = json.dumps(cm_layout, indent=4)
        annotated_cm_layout_dump = annotate_cm_layout_dump(cm_layout_dump)
        print(annotated_cm_layout_dump)

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
    stage_ids = list_stage_ids(root_path / "stage")
    if not validate_story_offsets(mainloop_buffer, world_offsets, stage_ids):
        logging.warning("Story mode offsets do not look valid for this ROM.")
        return
    worlds = []
    for offs in world_offsets:
        world = dump_storymode_world_layout(
            mainloop_buffer,
            stgname_lines,
            stage_id_to_theme_id_map,
            theme_id_to_music_id_map,
            offs,
        )
        worlds.append(world)

    story_layout_dump = json.dumps(worlds, indent=4)
    annotated_story_layout_dump = annotate_story_layout_dump(story_layout_dump)
    # print(annotated_story_layout_dump)


if __name__ == "__main__":
    main()
