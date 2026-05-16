; sidviz_d64.asm — standalone D64 player
; 64tass assembler  —  autostart: SYS 2064
;
; v1.9.4 (2026-05-12)
;
; PRG layout (Python assembles final file by appending after $09AF):
;   $0801-$080C  BASIC stub (SYS 2064)
;   $0810-$09AF  Player code  (this file, exactly 416 bytes)
;   $09B0-$09BF  Metadata (16 bytes, Python fills in):
;                  [0-1]  SID init address (LE)
;                  [2-3]  SID play address (LE; $0000 = self-installs via IRQ)
;                  [4-5]  frame count (LE)
;                  [6]    fps_divisor = 50/fps  (e.g. 5 for 10fps)
;                  [7]    SIDDATA filename length
;                  [8-15] SIDDATA filename (PETSCII, $A0-padded, 8 bytes)
;   $09C0-$09C2  Init trampoline  JMP initAddr  (Python fills)
;   $09C3-$09C5  Play trampoline  JMP playAddr  (Python fills)
;   $09C6        Frame-data base page (high byte of base addr; low byte always $00)
;   $09C7-...    Frame index: 2 bytes/frame (LE byte-offset from fdat base)
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
; ZP $B0-$BD (unused by sidviz.prg which only runs in live mode):
;   $B0/$B1  src_lo/src_hi   read pointer (compressed data)
;   $B2/$B3  dst_lo/dst_hi   write pointer (screen RAM)
;   $B4      frame_tick      IRQ countdown until next frame
;   $B5      new_frame       1 = advance + display next frame (set by IRQ)
;   $B6/$B7  cur_lo/cur_hi   current frame number
;   $B8/$B9  orig_irq_lo/hi  saved $0314/$0315
;   $BA      scratch
;   $BB/$BC  fidx_lo/fidx_hi frame-index base pointer
;   $BD      fdat_page       high byte of frame-data base addr (low = $00)

src_lo      = $b0
src_hi      = $b1
dst_lo      = $b2
dst_hi      = $b3
frame_tick  = $b4
new_frame   = $b5
cur_lo      = $b6
cur_hi      = $b7
orig_irq_lo = $b8
orig_irq_hi = $b9
scratch     = $ba
fidx_lo     = $bb
fidx_hi     = $bc
fdat_page   = $bd

screen      = $0400
color_ram   = $d800
border      = $d020
bgcol       = $d021
irq_vec_lo  = $0314
irq_vec_hi  = $0315

wave_scr    = $0540     ; screen row 8 — waveform area start
; End of waveform area: $0540 + 680 = $07E8

SETNAM      = $ffbd
SETLFS      = $ffba
KLOAD       = $ffd5

META_INIT   = $09c0
META_PLAY   = $09c2
META_FCOUNT = $09c4
META_FPSDIV = $09c6
META_NAMLEN = $09c7
META_NAME   = $09c8     ; 8 bytes PETSCII filename
TRAM_INIT   = $09d0     ; Python writes: $4C lo hi
TRAM_PLAY   = $09d3     ; Python writes: $4C lo hi
FDAT_PAGE_ADDR = $09d6
FRAME_IDX   = $09d7

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

        ; disable BASIC ROM ($A000-$BFFF → RAM) while keeping KERNAL+I/O
        ; $01 bits: 0=LORAM 1=HIRAM 2=CHAREN  ($36 = LORAM off, HIRAM+CHAREN on)
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
cls_rm: sta (dst_lo),y  ; last 232 bytes of page $07
        iny
        cpy #232
        bne cls_rm

        ; fill color RAM $D800-$DBE7 white (1) — same structure
        lda #<color_ram
        sta dst_lo
        lda #>color_ram
        sta dst_hi
        lda #$01
        ldx #$03
col_pg: ldy #$00
col_lp: sta (dst_lo),y
        iny
        bne col_lp
        inc dst_hi
        dex
        bne col_pg
        ldy #$00
col_rm: sta (dst_lo),y
        iny
        cpy #232
        bne col_rm

        ; init new_frame only (other ZP vars clobbered by KERNAL below; re-init after KLOAD)
        lda #$00
        sta new_frame

        ; KERNAL LOAD: SIDDATA from disk (SA=1 → loads to file's embedded address)
        ; NOTE: SETNAM clobbers $B7(FNLEN)=cur_hi, $BB/$BC(FNADR)=fidx_lo/hi
        ;       SETLFS clobbers $B8(LA)=orig_irq_lo, $B9(SA)=orig_irq_hi
        ;       All are re-initialised after KLOAD below.
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

        lda #<FRAME_IDX
        sta fidx_lo
        lda #>FRAME_IDX
        sta fidx_hi

        lda FDAT_PAGE_ADDR
        sta fdat_page

        ; save KERNAL IRQ vector (after KLOAD so $B8/$B9 are free)
        lda irq_vec_lo
        sta orig_irq_lo
        lda irq_vec_hi
        sta orig_irq_hi

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
        jmp (orig_irq_lo)

; ---------------------------------------------------------------------------
; decomp_frame
;   Reads cur_lo/cur_hi, looks up byte-offset in FRAME_IDX,
;   then RLE-decompresses frame into screen RAM $0540-$07E7.
; ---------------------------------------------------------------------------

decomp_frame:
        ; address of index entry = fidx + cur*2
        lda cur_lo
        asl
        pha                 ; save low byte of cur*2
        lda cur_hi
        rol                 ; A = high byte of cur*2

        clc
        adc fidx_hi
        sta src_hi

        pla                 ; A = low byte of cur*2
        clc
        adc fidx_lo
        sta src_lo
        bcc no_idx_carry
        inc src_hi
no_idx_carry:

        ; read 2-byte frame offset from index
        ldy #$00
        lda (src_lo),y      ; offset_lo
        pha
        ldy #$01
        lda (src_lo),y      ; offset_hi
        sta scratch

        ; frame data ptr = fdat_page:$00 + offset
        ; (Python ensures frame data starts on a page boundary, so base_lo=$00)
        pla
        sta src_lo          ; src_lo = offset_lo  (adds to $00, no carry)
        lda scratch
        clc
        adc fdat_page
        sta src_hi

        ; destination = wave_scr ($0540)
        lda #<wave_scr
        sta dst_lo
        lda #>wave_scr
        sta dst_hi

; ── RLE loop ─────────────────────────────────────────────────────────────────
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
        lda (src_lo),y      ; token byte
        jsr inc_src
        bmi do_run

        ; $00-$7F: count byte — next (A+1) bytes are literals
        tax
        inx                 ; X = literal count (1..128)
lit_lp:
        lda (src_lo),y      ; literal byte (y=0)
        sta (dst_lo),y
        jsr inc_src
        jsr inc_dst
        dex
        bne lit_lp
        jmp decomp_lp

do_run:
        ; $80-$FF: run — repeat next byte (A - $7E) times
        sec
        sbc #$7e
        tax
        lda (src_lo),y      ; run value (y=0)
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
; Pad so that metadata lands at $09B0
; ---------------------------------------------------------------------------

        .fill $09c0 - *, $00
