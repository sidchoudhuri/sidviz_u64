; sidviz_d64.asm — standalone D64 player
; 64tass assembler  —  autostart: SYS 2064
;
; v1.9.6 (2026-05-16)
;
; PRG layout (Python assembles final file by appending after $09D0):
;   $0801-$080C  BASIC stub (SYS 2064)
;   $0810-$09CF  Player code  (this file, padded to $09D0 with .fill)
;   $09D0-$09DF  Metadata (16 bytes, Python fills):
;                  [0-1]  SID init address (LE)
;                  [2-3]  SID play address (LE; $0000 = self-installs via IRQ)
;                  [4-5]  frame count (LE)
;                  [6]    fps_divisor = 50/fps  (e.g. 5 for 10fps)
;                  [7]    SIDDATA filename length
;                  [8-15] SIDDATA filename (PETSCII, $A0-padded, 8 bytes)
;   $09E0-$09E2  Init trampoline  JMP initAddr  (Python fills)
;   $09E3-$09E5  Play trampoline  JMP playAddr  (Python fills)
;   $09E6        Frame-data base page (high byte of base addr; low = $00)
;   $09E7        Ticker string length (1 byte, Python fills)
;   $09E8-$0AE4  Ticker string (253 bytes PETSCII, Python fills, $20-padded)
;   $0AE5-...    Frame index: 2 bytes/frame (LE byte-offset from fdat base)
;                Python appends frame_count*2 bytes, then pads to next page boundary
;   ...          Compressed frame data (Python appends)
;
; Second file "SIDDATA" on disk = 2-byte LE load-addr + raw SID binary.
; Player loads it at boot with KERNAL LOAD (SA=1).
;
; RLE token format:
;   $00-$7F  count byte — the next (count+1) bytes are literals (1..128 bytes)
;   $80-$FF  run marker — repeat the next byte  (value - $7E) times  (2..129 reps)
;
; ZP $B0-$BD:
;   $B0/$B1  src_lo/src_hi   read pointer (compressed data)
;   $B2/$B3  dst_lo/dst_hi   write pointer (screen RAM)
;   $B4      frame_tick      IRQ countdown until next frame
;   $B5      new_frame       1 = advance + display next frame (set by IRQ)
;   $B6/$B7  cur_lo/cur_hi   current frame number
;   $B8      ticker_pos      current read position in ticker string
;   $BD      fdat_page       high byte of frame-data base addr (low = $00)
;
; irq_chain ($08FD in code RAM) is self-patched by init with the KERNAL IRQ
; vector so a SID using ZP $B8/$B9 cannot corrupt the IRQ chain target.
; FRAME_IDX is an inline immediate; fdat_page is reloaded from FDAT_PAGE_ADDR
; each frame — both safe from SID ZP collisions.

src_lo      = $b0
src_hi      = $b1
dst_lo      = $b2
dst_hi      = $b3
frame_tick  = $b4
new_frame   = $b5
cur_lo      = $b6
cur_hi      = $b7
ticker_pos  = $b8
fdat_page   = $bd

screen      = $0400
color_ram   = $d800
border      = $d020
bgcol       = $d021
irq_vec_lo  = $0314
irq_vec_hi  = $0315

wave_scr    = $0540     ; screen row 8 — waveform area start

SETNAM      = $ffbd
SETLFS      = $ffba
KLOAD       = $ffd5

META_INIT      = $09d0
META_PLAY      = $09d2
META_FCOUNT    = $09d4
META_FPSDIV    = $09d6
META_NAMLEN    = $09d7
META_NAME      = $09d8  ; 8 bytes PETSCII filename
TRAM_INIT      = $09e0  ; Python writes: $4C lo hi
TRAM_PLAY      = $09e3  ; Python writes: $4C lo hi
FDAT_PAGE_ADDR = $09e6
TICKER_LEN     = $09e7  ; 1 byte: actual ticker string length (≤253)
TICKER_BUF     = $09e8  ; 253 bytes: PETSCII ticker string, $20-padded
FRAME_IDX      = $0ae5  ; 2 bytes/frame LE offsets from fdat base

; ---------------------------------------------------------------------------
; BASIC stub: 10 SYS 2064
; ---------------------------------------------------------------------------

* = $0801
        .word next_line, 10
        .byte $9e
        .text "2064"
        .byte $00
next_line
        .word $0000

; ---------------------------------------------------------------------------
; Entry point $0810
; ---------------------------------------------------------------------------

* = $0810

init:
        sei

        ; disable BASIC ROM ($A000-$BFFF → RAM) keeping KERNAL+I/O
        ; $01 bits: LORAM off, HIRAM+CHAREN on ($36)
        lda #$36
        sta $01

        lda #$00
        sta border
        sta bgcol

        ; clear screen RAM $0400-$07E7 (3 full pages + 232 bytes)
        lda #<screen
        sta dst_lo
        lda #>screen
        sta dst_hi
        lda #$20
        ldx #$03
cls_pg: ldy #$00
cls_lp: sta (dst_lo),y
        iny
        bne cls_lp
        inc dst_hi
        dex
        bne cls_pg
        ldy #$00
cls_rm: sta (dst_lo),y
        iny
        cpy #232
        bne cls_rm

        ; init ZP vars before KERNAL calls clobber $B7-$BC
        lda #$00
        sta new_frame
        sta ticker_pos

        ; KERNAL LOAD: SIDDATA from disk (SA=1 → loads to embedded address)
        ; SETNAM clobbers $B7/$BB/$BC; SETLFS clobbers $B8/$B9 — all re-inited below
        lda META_NAMLEN
        ldx #<META_NAME
        ldy #>META_NAME
        jsr SETNAM

        lda #$01
        ldx #$08
        ldy #$01
        jsr SETLFS

        lda #$00
        ldx #$00
        ldy #$00
        jsr KLOAD

        ; re-init ZP vars clobbered by KERNAL file routines
        lda #$00
        sta cur_lo
        sta cur_hi

        ; patch IRQ chain with KERNAL's original IRQ vector (before SID init
        ; changes $0314/$0315) — stored in code RAM, safe from SID ZP use
        lda irq_vec_lo
        sta irq_chain+1
        lda irq_vec_hi
        sta irq_chain+2

        ; call SID init
        jsr TRAM_INIT

        ; if play_addr==0: SID installed play at $0314/$0315 — capture it
        lda META_PLAY
        ora META_PLAY+1
        bne install_irq

        lda #$4c
        sta TRAM_PLAY
        lda irq_vec_lo
        sta TRAM_PLAY+1
        lda irq_vec_hi
        sta TRAM_PLAY+2

install_irq:
        lda #<irq_handler
        sta irq_vec_lo
        lda #>irq_handler
        sta irq_vec_hi

        lda META_FPSDIV
        sta frame_tick

        cli

; ---------------------------------------------------------------------------
; Main loop
; ---------------------------------------------------------------------------

main:
        lda new_frame
        beq main

        lda #$00
        sta new_frame

        jsr scroll_ticker
        jsr decomp_frame

        inc cur_lo
        bne chk_wrap
        inc cur_hi
chk_wrap:
        lda cur_hi
        cmp META_FCOUNT+1
        bcc main
        bne do_wrap
        lda cur_lo
        cmp META_FCOUNT
        bcc main
do_wrap:
        lda #$00
        sta cur_lo
        sta cur_hi
        jmp main

; ---------------------------------------------------------------------------
; IRQ handler — 50 Hz (PAL); calls SID play, ticks frame counter
; ---------------------------------------------------------------------------

irq_handler:
        pha
        txa
        pha
        tya
        pha

        lda $b0
        pha
        lda $b1
        pha
        lda $b2
        pha
        lda $b3
        pha

        jsr TRAM_PLAY

        pla
        sta $b3
        pla
        sta $b2
        pla
        sta $b1
        pla
        sta $b0

        dec frame_tick
        bne irq_done

        lda META_FPSDIV
        sta frame_tick
        lda #$01
        sta new_frame

irq_done:
        pla
        tay
        pla
        tax
        pla
; irq_chain: self-patched by init to JMP <KERNAL IRQ vector>
irq_chain:
        .byte $4c, $00, $00

; ---------------------------------------------------------------------------
; decomp_frame — look up frame in FRAME_IDX, RLE-decomp to $0540-$07E7
; ---------------------------------------------------------------------------

decomp_frame:
        ; Reload fdat_page each frame (safe if SID corrupts ZP $BD)
        lda FDAT_PAGE_ADDR
        sta fdat_page

        ; index entry address = FRAME_IDX + cur*2  (FRAME_IDX as immediate)
        lda cur_lo
        asl
        pha                 ; save low byte of cur*2
        lda cur_hi
        rol                 ; A = high byte of cur*2

        clc
        adc #>FRAME_IDX
        sta src_hi

        pla
        clc
        adc #<FRAME_IDX
        sta src_lo
        bcc no_idx_carry
        inc src_hi
no_idx_carry:

        ; read 2-byte LE frame offset from index
        ldy #$00
        lda (src_lo),y      ; offset_lo
        pha
        ldy #$01
        lda (src_lo),y      ; offset_hi — use X (IRQ saves/restores X)
        tax

        ; frame data ptr = fdat_page:$00 + offset
        pla
        sta src_lo
        txa
        clc
        adc fdat_page
        sta src_hi

        ; first byte = C64 color for waveform rows 8-24
        ldy #$00
        lda (src_lo),y
        jsr inc_src
        ldy #191
cfill1: sta $d940,y
        dey
        bpl cfill1
        ldy #255
cfill2: sta $da00,y
        dey
        bpl cfill2
        ldy #231
cfill3: sta $db00,y
        dey
        bpl cfill3
        ldy #$00

        lda #<wave_scr
        sta dst_lo
        lda #>wave_scr
        sta dst_hi

decomp_lp:
        lda dst_hi
        cmp #$07
        bcc decomp_body
        bne decomp_done
        lda dst_lo
        cmp #$e8
        bcs decomp_done

decomp_body:
        ldy #$00
        lda (src_lo),y
        jsr inc_src
        bmi do_run

        tax
        inx
lit_lp:
        lda (src_lo),y
        sta (dst_lo),y
        jsr inc_src
        jsr inc_dst
        dex
        bne lit_lp
        jmp decomp_lp

do_run:
        sec
        sbc #$7e
        tax
        lda (src_lo),y
        jsr inc_src
run_lp:
        sta (dst_lo),y
        jsr inc_dst
        dex
        bne run_lp
        jmp decomp_lp

decomp_done:
        rts

; ---------------------------------------------------------------------------
; scroll_ticker — shift row 0 left one char, append next ticker char
; ---------------------------------------------------------------------------

scroll_ticker:
        ldx #0
stk_lp:
        lda screen+1,x      ; copy char x+1 → x  (shift left)
        sta screen,x
        inx
        cpx #39
        bne stk_lp
        ldy ticker_pos
        lda TICKER_BUF,y    ; next char from ticker string
        iny
        cpy TICKER_LEN      ; wrap at end of string
        bcc stk_ok
        ldy #0
stk_ok:
        sty ticker_pos
        sta screen+39       ; write to rightmost position
        rts

; ---------------------------------------------------------------------------
; Helpers
; ---------------------------------------------------------------------------

inc_src:
        inc src_lo
        bne inc_src_rts
        inc src_hi
inc_src_rts:
        rts

inc_dst:
        inc dst_lo
        bne inc_dst_rts
        inc dst_hi
inc_dst_rts:
        rts

; ---------------------------------------------------------------------------
; Pad so that metadata lands at $09D0
; ---------------------------------------------------------------------------

        .fill $09d0 - *, $00
