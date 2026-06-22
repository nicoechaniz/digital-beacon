#!/usr/bin/env python3
"""Standalone MIDI sniffer — touch pads to see note→position mapping."""
import sys
import mido

def main():
    # Find Launchpad
    in_names = mido.get_input_names()
    lp_name = None
    for n in in_names:
        if "Launchpad" in n or "LP" in n.upper():
            lp_name = n
            break
    if not lp_name:
        print("No Launchpad found. Available MIDI inputs:")
        for n in in_names:
            print(f"  {n}")
        sys.exit(1)

    print(f"Opening: {lp_name}")
    print("Touch the 4 CORNERS first (bottom-left, bottom-right, top-left, top-right), then CENTER.")
    print("Press Ctrl-C to exit.\n")

    with mido.open_input(lp_name) as port:
        print("Ready — touch pads now:\n")
        for msg in port:
            if msg.type != 'note_on' or msg.velocity == 0:
                continue
            note = msg.note
            # In stride 16: 8 columns × 8 rows, note 0 starts bottom-left
            x = note % 8
            y = note // 8  # row from bottom

            # Also compute row from top (assuming 8×8 grid)
            y_top = 7 - y

            # Descriptive position
            if x == 0 and y == 0:
                desc = "BOTTOM-LEFT ★"
            elif x == 7 and y == 0:
                desc = "BOTTOM-RIGHT ★"
            elif x == 0 and y == 7:
                desc = "TOP-LEFT ★"
            elif x == 7 and y == 7:
                desc = "TOP-RIGHT ★"
            elif x in (3, 4) and y in (3, 4):
                desc = "CENTER ★"
            else:
                desc = ""

            # Launchpad Mini grid labeling: A-H rows (A=top), 1-8 columns (1=left)
            row_letter = chr(ord('H') - y) if 0 <= y <= 7 else '?'
            col_num = x + 1
            grid_label = f"{row_letter}{col_num}"

            # Row from top (A=0)
            row_top_letter = chr(ord('A') + y_top) if 0 <= y_top <= 7 else '?'

            print(f"  note={note:3d}  x={x}  y(bottom)={y}  y(top)={y_top}  "
                  f"grid(label)={grid_label}  row_top={row_top_letter}  {desc}")

if __name__ == "__main__":
    main()
