# combine.py
import os
import json
import argparse
from tqdm import tqdm
import sys
from utils.logger_util import get_logger, setup_logging, color_message

@get_logger
def main(logger, args):
    # Output path: parent of temp_dir + model_name_{lang}.jsonl
    parent_dir = os.path.dirname(os.path.abspath(args.temp_dir.rstrip("/")))
    if args.output_file:
        out_jsonl = args.output_file
    else:   
        # <--- MODIFIED: Use the 'lang' argument to dynamically set the output filename.
        out_jsonl = os.path.join(parent_dir, f"{args.model_name}_{args.lang}.jsonl")

    files = [f for f in os.listdir(args.temp_dir) if f.endswith(".json")]
    files.sort()

    total_files = len(files)
    left_index = args.left
    # If right is default -1, process to end of list; otherwise process to the specified position
    right_index = args.right if args.right != -1 else total_files
    
    # Ensure indices are within bounds
    files_to_process = files[max(0, left_index):min(total_files, right_index)]
    
    logger.info(f"Found {total_files} files, will process {len(files_to_process)} files from index {left_index} to {right_index}.")


    with open(out_jsonl, "w", encoding="utf-8") as out:
        for fn in tqdm(files_to_process, desc="Processing json files", file=sys.stderr):
            p = os.path.join(args.temp_dir, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"Skipping {fn}, read failed. Reason: {e}")
    logger.info(f"Total {len(files_to_process)} tasks written to {out_jsonl}")



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--temp_dir", default="temp", help="Directory containing temporary JSON files")
    ap.add_argument("--model_name", required=True, help="Model name, used for output filename")
    ap.add_argument("--output_file", default="", help="Output file path")
    ap.add_argument("--lang", default="c", choices=['c', 'cpp'], help="Language (c or cpp) for output filename")
    ap.add_argument("--left", type=int, default=0, help="Start index, default 0")
    ap.add_argument("--right", type=int, default=-1, help="End index, default -1 (all)")
    args = ap.parse_args()
    setup_logging("log/pipeline", "combine.log")
    main(args)

