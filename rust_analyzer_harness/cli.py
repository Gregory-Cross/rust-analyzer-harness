#!/usr/bin/env python3
"""
Simplified Rust Analyzer Interface for Claude Code
Streamlined for LLM usage with minimal syntax
"""

import sys
import json
import re
from pathlib import Path

from .core import PersistentRustAnalyzer

def parse_args(args):
    """Parse command line arguments"""
    file_path = None
    line_number = None
    show_full = False
    errors_only = False
    show_all = False
    
    for arg in args:
        if arg == "--full":
            show_full = True
        elif arg == "--errors":
            errors_only = True
        elif arg == "--all":
            show_all = True
        elif ":" in arg and not arg.startswith("--"):
            # Parse file:line format
            parts = arg.split(":")
            file_path = Path(parts[0])
            if len(parts) > 1 and parts[1].isdigit():
                line_number = int(parts[1])
        elif not arg.startswith("--"):
            file_path = Path(arg)
    
    return file_path, line_number, show_full, errors_only, show_all

def format_diagnostic(d, file_name, show_full=False):
    """Format a single diagnostic for output"""
    severity = ["Error", "Warning", "Info", "Hint"][d.get("severity", 1) - 1]
    line = d['range']['start']['line'] + 1
    col = d['range']['start']['character'] + 1
    msg = d['message']
    
    # For errors, always show the basic info
    output = []
    
    if severity == "Error":
        output.append(f"\n{file_name}:{line}:{col}: **{severity}** - {msg}")
    else:
        output.append(f"\n{file_name}:{line}:{col}: {severity} - {msg}")
    
    if show_full or severity == "Error":
        # Show error code
        if 'code' in d:
            code_desc = d.get('codeDescription', {})
            if 'href' in code_desc:
                output.append(f"  Error code: [{d['code']}]({code_desc['href']})")
            else:
                output.append(f"  Error code: {d['code']}")
        
        # Show source
        if 'source' in d:
            output.append(f"  Source: {d['source']}")
        
        # Show related information for errors
        if 'relatedInformation' in d and severity == "Error":
            output.append(f"  Related:")
            for rel in d['relatedInformation']:
                rel_loc = rel.get('location', {})
                rel_uri = rel_loc.get('uri', '')
                rel_range = rel_loc.get('range', {})
                if rel_uri and rel_range:
                    rel_file = Path(rel_uri.replace("file://", "")).name
                    rel_line = rel_range['start']['line'] + 1
                    output.append(f"    - {rel_file}:{rel_line}: {rel['message']}")
                else:
                    output.append(f"    - {rel['message']}")
    
    return "\n".join(output)

def main():
    # Parse arguments
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    
    if not args or args[0] in ["--help", "-h", "help"]:
        print("Rust Analyzer - Simplified Interface")
        print("\nUsage:")
        print("  ra <file>              Show errors and warnings in file")
        print("  ra <file> --full       Show all diagnostics with full details")
        print("  ra <file>:line         Show diagnostics around specific line")
        print("  ra --all               Show all errors in project")
        print("  ra --errors            Show only errors (no warnings)")
        print("\nExamples:")
        print("  ra src/main.rs")
        print("  ra src/main.rs:42")
        print("  ra src/main.rs --full")
        print("  ra --all --errors")
        return
    
    file_path, line_number, show_full, errors_only, show_all = parse_args(args)
    
    try:
        # Get rust-analyzer instance
        analyzer = PersistentRustAnalyzer(Path.cwd())
        analyzer.start()
        
        # Get diagnostics
        if show_all:
            result = analyzer.get_diagnostics()
        elif file_path:
            result = analyzer.get_diagnostics(file_path)
        else:
            print("Error: No file specified", file=sys.stderr)
            sys.exit(1)
        
        # Count diagnostics
        error_count = 0
        warning_count = 0
        total_count = 0
        
        for uri, diags in result.items():
            for d in diags:
                severity = d.get("severity", 1)
                if severity == 1:  # Error
                    error_count += 1
                elif severity == 2:  # Warning
                    warning_count += 1
                total_count += 1
        
        # Print summary
        if error_count > 0:
            print(f"**{error_count} error{'s' if error_count > 1 else ''}**", end="")
            if warning_count > 0 and not errors_only:
                print(f", {warning_count} warning{'s' if warning_count > 1 else ''}")
            else:
                print()
        elif warning_count > 0 and not errors_only:
            print(f"{warning_count} warning{'s' if warning_count > 1 else ''}")
        else:
            print("No issues found")
            return
        
        # Print diagnostics
        for uri, diags in result.items():
            if diags:
                file_name = Path(uri.replace("file://", "")).name
                
                # Filter by line number if specified
                if line_number:
                    diags = [d for d in diags 
                            if abs(d['range']['start']['line'] + 1 - line_number) <= 5]
                
                # Filter by severity if errors_only
                if errors_only:
                    diags = [d for d in diags if d.get("severity", 1) == 1]
                
                # Sort by line number
                diags.sort(key=lambda d: (d['range']['start']['line'], d['range']['start']['character']))
                
                for d in diags:
                    print(format_diagnostic(d, file_name, show_full))
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()