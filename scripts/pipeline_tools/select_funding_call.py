import argparse
import json
import sys
from pathlib import Path

from core.funding_selection import ApplicantProfile, execute_selection, DEFAULT_DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Select a funding call for the pipeline.")
    parser.add_argument("--mode", choices=["autonomous", "review"], required=True, help="Selection mode")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to funding SQLite database")
    parser.add_argument("--output-dir", default="outputs/workflow", help="Output directory")
    parser.add_argument("--select", help="Call ID to select when in review mode")
    parser.add_argument("--min-trl", type=int, default=1, help="Minimum TRL")
    parser.add_argument("--max-trl", type=int, default=9, help="Maximum TRL")
    parser.add_argument("--applicant-type", default="academic", help="Applicant type (academic, startup, etc.)")
    parser.add_argument("--target-areas", help="Comma-separated target research areas")
    parser.add_argument("--allowed-geographies", help="Comma-separated allowed geographies")

    args = parser.parse_args()

    # Build profile
    profile_args = {
        "min_trl": args.min_trl,
        "max_trl": args.max_trl,
        "applicant_type": args.applicant_type,
    }
    if args.target_areas:
        profile_args["target_research_areas"] = [t.strip() for t in args.target_areas.split(",") if t.strip()]
    if args.allowed_geographies:
        profile_args["allowed_geographies"] = [g.strip() for g in args.allowed_geographies.split(",") if g.strip()]

    profile = ApplicantProfile(**profile_args)

    try:
        result = execute_selection(
            mode=args.mode,
            profile=profile,
            db_path=Path(args.db_path),
            output_dir=Path(args.output_dir),
            interactive_choice=args.select
        )
        print(json.dumps(result, indent=2))
        
        if result.get("status") == "NO_SUITABLE_CALL":
            return 1
            
        return 0
    except Exception as exc:
        print(f"Error during selection: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
