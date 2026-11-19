; reuload.asm - sidviz's REU loader for the Ultimate (64tass)
; sidviz sends a .reu in blocks into C64 RAM with the REST API (machine:writemem);
; this copies each block into the REU, then starts the video player.
;
; Mailbox at $02A7 (sidviz writes it after the block):
;   +0  command: 1 = copy $2000.. into the REU, 2 = copy the REU into $2000.. (a check),
;                3 = start the player at $080E (sidviz has put it at $0801); 0 = done
;   +1  REU address low, +2 middle, +3 bank
;   +4  length low, +5 high (0 = 64 KB)
;
; The code runs from the cassette buffer ($0340), clear of $0801 (the player) and $2000-$9FFF
; (the blocks). The border shows each block as it goes in.

mbox    = $02a7
reu     = $df00
buffer  = $2000
player  = $080e

        * = $0801
        .word (+), 2026
        .null $9e, format("%d", start)
+       .word 0

start:  ldx #code_end - code - 1
cp:     lda code_src,x
        sta code,x
        dex
        bpl cp
        jmp code

code_src:
        .logical $0340
code:   sei
        lda #0
        sta mbox
        sta $d020
wait:   lda mbox
        beq wait
        cmp #3
        beq go
        lda #<buffer
        sta reu+2
        lda #>buffer
        sta reu+3
        lda mbox+1
        sta reu+4
        lda mbox+2
        sta reu+5
        lda mbox+3
        sta reu+6
        lda mbox+4
        sta reu+7
        lda mbox+5
        sta reu+8
        lda #0
        sta reu+10              ; both addresses count up
        lda mbox
        cmp #2
        beq fetch
        lda #$90                ; stash (C64 -> REU), now
        bne do
fetch:  lda #$91                ; fetch (REU -> C64), now
do:     sta reu+1
        inc $d020
        lda #0
        sta mbox
        beq wait
go:     lda #0
        sta mbox
        cli
        jsr player              ; as SYS would; back to BASIC if it returns
        jmp $e37b
code_end:
        .endlogical
