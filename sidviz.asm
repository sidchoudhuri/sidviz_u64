; sidviz.asm
; 64tass assembler
; autostart SYS 2064
;
; version 1.1.1 (2026-04-17-2)
;
; Memory map:
;   $C000     = frame ready flag  (Python writes 1, ASM clears to 0)
;   $C001     = color toggle flag (Python writes 1=white, 2=rainbow, ASM applies+clears)
;   $C100     = frame buffer, 920 bytes PETSCII (rows 2-24, 23 rows x 40 cols)
;               frame buffer occupies $C100-$C497
;   $C500     = ticker buffer, up to 253 PETSCII chars (Python writes)
;   $C5FD     = irq_tick counter (ASM owns)
;   $C5FE     = ticker length (Python writes)
;   $C5FF     = ticker read position (ASM owns)
;
; Screen layout:
;   Row 0:    blank (black)
;   Row 1:    scrolling metadata ticker ($0428, light green color 13)
;   Rows 2-24: waveform ($0450-$07E7, 23 rows x 40 cols = 920 bytes)
;
; ZP usage:
;   $F9/$FA   = saved original IRQ vector (for JMP indirect)
;   $FB/$FC   = src pointer (used by copy_frame and fill routines)
;   $FD/$FE   = dst pointer (used by copy_frame and fill routines)

; ---------------------------------------------------------------------------
; Zero page
; ---------------------------------------------------------------------------

src_lo      = $fb
src_hi      = $fc
dst_lo      = $fd
dst_hi      = $fe
orig_irq_lo = $f9       ; saved IRQ vector lo (ZP for JMP indirect)
orig_irq_hi = $fa       ; saved IRQ vector hi

; ---------------------------------------------------------------------------
; Hardware
; ---------------------------------------------------------------------------

screen      = $0400
color       = $d800
border      = $d020
bgcol       = $d021
irq_vec_lo  = $0314
irq_vec_hi  = $0315

; Row addresses (screen + N*40)
ticker_scr  = $0428     ; row 1  (screen + 1*40)
ticker_col  = $d828     ; row 1 color RAM
wave_scr    = $0450     ; row 2  (screen + 2*40)
wave_col    = $d850     ; row 2 color RAM

; ---------------------------------------------------------------------------
; Python comms
; ---------------------------------------------------------------------------

frame_flag  = $c000
color_flag  = $c001
frame_buf   = $c100     ; 920 bytes, occupies $C100-$C497

ticker_buf  = $c500     ; up to 253 PETSCII chars
irq_tick    = $c5fd     ; IRQ scroll rate counter (ASM owns)
ticker_len  = $c5fe     ; ticker string length (Python writes)
ticker_pos  = $c5ff     ; current read position (ASM owns)

SCROLL_RATE = 6

; ---------------------------------------------------------------------------
; BASIC stub: SYS 2064 ($0810)
; ---------------------------------------------------------------------------

* = $0801
        .word line10, 10
        .byte $9e
        .text "2064"
        .byte 0
line10  .word 0

; ---------------------------------------------------------------------------
; Program entry
; ---------------------------------------------------------------------------

* = $0810

init:
        sei

        ; Black border and background
        lda #$00
        sta border
        sta bgcol

        ; Clear comms flags
        sta frame_flag
        sta color_flag
        sta ticker_pos

        ; Init IRQ tick counter
        lda #SCROLL_RATE
        sta irq_tick

        ; Set up display
        jsr fill_color_rainbow  ; rows 2-24 rainbow colors
        jsr fill_ticker_color   ; row 1 light green
        jsr fill_row0_color     ; row 0 black
        jsr clear_screen        ; all spaces

        ; Pre-fill ticker buffer with spaces so IRQ scrolls blanks
        ; until Python sends real ticker data
        lda #$20
        ldy #0
ptb_lp: sta ticker_buf,y
        iny
        bne ptb_lp
        ; Also set ticker_len to 40 (one screen width of spaces)
        lda #40
        sta ticker_len

        ; Hook IRQ — store original vector in ZP for JMP (indirect)
        lda irq_vec_lo
        sta orig_irq_lo
        lda irq_vec_hi
        sta orig_irq_hi
        lda #<irq_handler
        sta irq_vec_lo
        lda #>irq_handler
        sta irq_vec_hi

        cli

; ---------------------------------------------------------------------------
; Main loop — poll color_flag and frame_flag
; ---------------------------------------------------------------------------

main_loop:
        lda color_flag
        beq check_frame
        cmp #$01
        bne do_rainbow
        jsr fill_color_white
        jmp clr_cflag
do_rainbow:
        jsr fill_color_rainbow
clr_cflag:
        lda #$00
        sta color_flag

check_frame:
        lda frame_flag
        beq main_loop
        jsr copy_frame
        jmp main_loop

; ---------------------------------------------------------------------------
; IRQ handler — saves/restores all registers
; Scrolls ticker row left one char every SCROLL_RATE IRQ ticks (~50Hz/3)
; ---------------------------------------------------------------------------

irq_handler:
        pha
        txa
        pha
        tya
        pha

        dec irq_tick
        bne irq_done

        lda #SCROLL_RATE
        sta irq_tick

        ; Shift ticker row left: col[n] = col[n+1] for n=0..38
        ldx #0
scroll_lp:
        lda ticker_scr+1,x
        sta ticker_scr,x
        inx
        cpx #39
        bne scroll_lp

        ; Get next char from circular buffer, advance position
        ldy ticker_pos
        lda ticker_buf,y
        iny
        cpy ticker_len
        bcc pos_ok
        ldy #0
pos_ok:
        sty ticker_pos

        ; Write new char into col 39
        sta ticker_scr+39

irq_done:
        pla
        tay
        pla
        tax
        pla
        jmp (orig_irq_lo)

; ---------------------------------------------------------------------------
; copy_frame: $C100-$C497 -> $0450-$07E7 (920 bytes = 23 rows)
; ---------------------------------------------------------------------------

copy_frame:
        lda #<frame_buf
        sta src_lo
        lda #>frame_buf
        sta src_hi
        lda #<wave_scr
        sta dst_lo
        lda #>wave_scr
        sta dst_hi

        ; Copy 3 full pages (768 bytes)
        ldx #3
cf_pg:  ldy #0
cf_lp:  lda (src_lo),y
        sta (dst_lo),y
        iny
        bne cf_lp
        inc src_hi
        inc dst_hi
        dex
        bne cf_pg

        ; Copy remaining 152 bytes (920 - 768)
        ldy #0
cf_rm:  lda (src_lo),y
        sta (dst_lo),y
        iny
        cpy #152
        bne cf_rm

        lda #0
        sta frame_flag
        rts

; ---------------------------------------------------------------------------
; clear_screen: fill $0400-$07E7 (1000 bytes) with space ($20)
; ---------------------------------------------------------------------------

clear_screen:
        lda #<screen
        sta dst_lo
        lda #>screen
        sta dst_hi
        lda #$20
        ldx #3
cs_pg:  ldy #0
cs_lp:  sta (dst_lo),y
        iny
        bne cs_lp
        inc dst_hi
        dex
        bne cs_pg
        ldy #0
cs_rm:  sta (dst_lo),y
        iny
        cpy #232
        bne cs_rm
        rts

; ---------------------------------------------------------------------------
; fill_color_rainbow: fill color RAM rows 2-24 ($D850-$DBE7, 920 bytes)
; Rainbow by column, X tracks col 0..39
; ---------------------------------------------------------------------------

fill_color_rainbow:
        lda #<wave_col
        sta dst_lo
        lda #>wave_col
        sta dst_hi
        ldx #0

        ldy #0
fcr_p0: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_p0n
        ldx #0
fcr_p0n:iny
        bne fcr_p0
        inc dst_hi

        ldy #0
fcr_p1: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_p1n
        ldx #0
fcr_p1n:iny
        bne fcr_p1
        inc dst_hi

        ldy #0
fcr_p2: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_p2n
        ldx #0
fcr_p2n:iny
        bne fcr_p2
        inc dst_hi

        ldy #0
fcr_rm: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_rmn
        ldx #0
fcr_rmn:iny
        cpy #152
        bne fcr_rm
        rts

; ---------------------------------------------------------------------------
; fill_color_white: fill color RAM rows 2-24 with white (1)
; ---------------------------------------------------------------------------

fill_color_white:
        lda #<wave_col
        sta dst_lo
        lda #>wave_col
        sta dst_hi
        lda #1
        ldx #3
fcw_pg: ldy #0
fcw_lp: sta (dst_lo),y
        iny
        bne fcw_lp
        inc dst_hi
        dex
        bne fcw_pg
        ldy #0
fcw_rm: sta (dst_lo),y
        iny
        cpy #152
        bne fcw_rm
        rts

; ---------------------------------------------------------------------------
; fill_ticker_color: fill color RAM row 1 ($D828, 40 bytes) with 13 (lt green)
; ---------------------------------------------------------------------------

fill_ticker_color:
        lda #<ticker_col
        sta dst_lo
        lda #>ticker_col
        sta dst_hi
        lda #13
        ldy #0
ftc_lp: sta (dst_lo),y
        iny
        cpy #40
        bne ftc_lp
        rts

; ---------------------------------------------------------------------------
; fill_row0_color: fill color RAM row 0 ($D800, 40 bytes) with 0 (black)
; ---------------------------------------------------------------------------

fill_row0_color:
        lda #<color
        sta dst_lo
        lda #>color
        sta dst_hi
        lda #0
        ldy #0
fr0_lp: sta (dst_lo),y
        iny
        cpy #40
        bne fr0_lp
        rts

; ---------------------------------------------------------------------------
; Rainbow table: 40 color indices, one per column
; ---------------------------------------------------------------------------

rainbow_table:
        .byte  2,  2,  8,  8,  7,  7,  7,  7
        .byte  5,  5,  5,  5, 13, 13, 14, 14
        .byte  6,  6,  6,  6,  4,  4,  4,  4
        .byte 10, 10,  2,  2,  8,  8,  7,  7
        .byte  5,  5, 13, 13, 14, 14,  6,  6
