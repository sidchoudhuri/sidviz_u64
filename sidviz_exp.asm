; sidviz_exp.asm
; 64tass assembler
; autostart SYS 2064
;
; version 1.4.0-exp (2026-04-17-exp1)
;
; EXPERIMENTAL: built-in PSID player
; Python parses PSID header, uploads SID code via writemem,
; patches sid_init_lo/hi and sid_play_lo/hi before running this PRG.
;
; Memory map:
;   $C000     = frame ready flag  (Python writes 1, ASM clears to 0)
;   $C001     = color toggle flag (2=rainbow, 1=white, 3=fire)
;   $C100     = frame buffer, 920 bytes PETSCII (rows 2-24)
;   $C500     = ticker buffer, up to 253 PETSCII chars
;   $C5FD     = irq_tick counter
;   $C5FE     = ticker length
;   $C5FF     = ticker read position
;   $C5FC     = color_mode (0=rainbow, 1=white, 2=fire)
;
; ZP usage:
;   $F7/$F8   = FREE (not used — play address is at $C610 trampoline)
;   $F9/$FA   = saved original IRQ vector
;   $FB/$FC   = src pointer
;   $FD/$FE   = dst pointer

; ---------------------------------------------------------------------------
; Zero page
; ---------------------------------------------------------------------------

orig_irq_lo = $f9
orig_irq_hi = $fa
src_lo      = $fb
src_hi      = $fc
dst_lo      = $fd
dst_hi      = $fe

; ---------------------------------------------------------------------------
; Hardware
; ---------------------------------------------------------------------------

screen      = $0400
color       = $d800
border      = $d020
bgcol       = $d021
irq_vec_lo  = $0314
irq_vec_hi  = $0315

ticker_scr  = $0428
ticker_col  = $d828
wave_scr    = $0540     ; row 8 — safely above SID driver at $0400-$04FF
wave_col    = $d940

; ---------------------------------------------------------------------------
; Python comms
; ---------------------------------------------------------------------------

frame_flag  = $c000
color_flag  = $c001
sid_ready   = $c002     ; Python sets 1 after uploading SID code
sid_tick    = $c003     ; SID play rate counter
frame_buf   = $c100
ticker_buf  = $c500
irq_tick    = $c5fd
ticker_len  = $c5fe
ticker_pos  = $c5ff
color_mode  = $c5fc

SCROLL_RATE = 6

; SID init address — Python patches this before running PRG
; Stored as a JMP instruction at $C600 so Python only needs to
; write 2 bytes for the address
sid_init_jmp = $c600       ; Python writes: $4C, lo, hi (JMP initAddress)
sid_init_rts = $c603       ; RTS after JMP so we can JSR to $C600... 
                           ; actually we just JSR sid_init_jmp which JMPs
                           ; to init — init ends with RTS returning here.
                           ; But JMP doesn't return... use trampoline instead.

; ---------------------------------------------------------------------------
; BASIC stub: SYS 2064
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
        sta color_mode
        sta ticker_pos
        sta sid_ready
        sta sid_tick

        ; Init IRQ tick counter
        lda #SCROLL_RATE
        sta irq_tick

        ; Set up display
        jsr fill_color_rainbow
        jsr fill_ticker_color
        jsr fill_row0_color
        jsr clear_screen

        ; Pre-fill ticker buffer with spaces
        lda #$20
        ldy #0
ptb_lp: sta ticker_buf,y
        iny
        bne ptb_lp
        lda #40
        sta ticker_len

        ; Save CURRENT IRQ vector (clean KERNAL vector, before SID init hooks it)
        lda irq_vec_lo
        sta orig_irq_lo
        lda irq_vec_hi
        sta orig_irq_hi

        ; Wait for Python to upload SID code and set sid_ready=$C002
wait_sid:   lda sid_ready
        beq wait_sid

        ; Call SID INIT — SID player will hook $0314/$0315 during init
        jsr sid_call_init

        ; Re-hook IRQ to our handler AFTER SID init
        ; orig_irq_lo/hi still points to clean KERNAL handler (pre-SID)
        ; so chaining skips the SID's $0314 hook — no double-play
        lda #<irq_handler
        sta irq_vec_lo
        lda #>irq_handler
        sta irq_vec_hi

        cli

; ---------------------------------------------------------------------------
; Main loop
; ---------------------------------------------------------------------------

main_loop:
        ; Call SID play if IRQ has flagged it
        lda sid_play_flag
        beq no_sid_play
        lda #$00
        sta sid_play_flag
        jsr sid_play_trampoline
no_sid_play:

        lda color_flag
        beq check_frame

        cmp #$02
        bne ml_not_rainbow
        jsr fill_color_rainbow
        lda #$00
        sta color_mode
        jmp clr_cflag

ml_not_rainbow:
        cmp #$01
        bne ml_not_white
        jsr fill_color_white
        lda #$01
        sta color_mode
        jmp clr_cflag

ml_not_white:
        jsr fill_color_white
        lda #$02
        sta color_mode

clr_cflag:
        lda #$00
        sta color_flag

check_frame:
        lda frame_flag
        beq main_loop
        jsr copy_frame
        lda color_mode
        beq main_loop
        cmp #$01
        beq do_density_white
        jsr density_colors_fire
        jmp main_loop
do_density_white:
        jsr density_colors
        jmp main_loop

; ---------------------------------------------------------------------------
; SID call trampoline
; sid_call_init: Python writes JMP initAddr at $C600
; We JSR $C600 — it JMPs to initAddr — initAddr RTSs back here
; This works because JSR pushes PC, JMP goes to init,
; init's RTS pops back to after our JSR. Clean.
; ---------------------------------------------------------------------------

sid_call_init:
        jsr $c600              ; $C600 contains JMP initAddress (patched by Python)
        rts

; ---------------------------------------------------------------------------
; IRQ handler — saves/restores all registers
; Sets sid_play_flag for main loop to call SID play outside IRQ context
; This avoids stack depth issues with complex SID play routines
; Scrolls ticker
; ---------------------------------------------------------------------------

sid_play_flag = $c003

irq_handler:
        pha
        txa
        pha
        tya
        pha

        ; Signal main loop to call SID play on next iteration
        lda #$01
        sta sid_play_flag

        ; Ticker scroll
        dec irq_tick
        bne irq_done

        lda #SCROLL_RATE
        sta irq_tick

        ldx #0
scroll_lp:
        lda ticker_scr+1,x
        sta ticker_scr,x
        inx
        cpx #39
        bne scroll_lp

        ldy ticker_pos
        lda ticker_buf,y
        iny
        cpy ticker_len
        bcc pos_ok
        ldy #0
pos_ok:
        sty ticker_pos
        sta ticker_scr+39

irq_done:
        pla
        tay
        pla
        tax
        pla
        ; Chain to original KERNAL IRQ (pre-SID vector) for housekeeping
        ; This is safe because we saved orig_irq_lo/hi BEFORE SID init
        ; so it does NOT call the SID's play hook at $0314
        jmp (orig_irq_lo)

; sid_play_trampoline: fixed address trampoline at $C610
; Python writes JMP playAddress ($4C lo hi) at $C610 before signalling ready
; No ZP involved — SID play routine cannot clobber our pointer
sid_play_trampoline:
        jmp $c610              ; $C610 contains JMP playAddress (patched by Python)

; ---------------------------------------------------------------------------
; copy_frame: $C100 -> $0540 (680 bytes = 17 rows, rows 8-24)
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

        ldx #2
cf_pg:  ldy #0
cf_lp:  lda (src_lo),y
        sta (dst_lo),y
        iny
        bne cf_lp
        inc src_hi
        inc dst_hi
        dex
        bne cf_pg

        ldy #0
cf_rm:  lda (src_lo),y
        sta (dst_lo),y
        iny
        cpy #168
        bne cf_rm

        lda #0
        sta frame_flag
        rts

; ---------------------------------------------------------------------------
; clear_screen
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
; fill_color_rainbow
; ---------------------------------------------------------------------------

fill_color_rainbow:
        lda #<wave_col
        sta dst_lo
        lda #>wave_col
        sta dst_hi
        ldx #0

        ; Page 0 (256 bytes)
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

        ; Page 1 (256 bytes) — total 512 bytes
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

        ; Remaining 168 bytes (680 - 512)
        ldy #0
fcr_rm: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_rmn
        ldx #0
fcr_rmn:iny
        cpy #168
        bne fcr_rm
        rts

; ---------------------------------------------------------------------------
; fill_color_white
; ---------------------------------------------------------------------------

fill_color_white:
        lda #<wave_col
        sta dst_lo
        lda #>wave_col
        sta dst_hi
        lda #1
        ldx #2
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
        cpy #168
        bne fcw_rm
        rts

; ---------------------------------------------------------------------------
; fill_ticker_color
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
; fill_row0_color
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
; density_colors (white palette)
; ---------------------------------------------------------------------------

density_colors:
        lda #<wave_scr
        sta src_lo
        lda #>wave_scr
        sta src_hi
        lda #<wave_col
        sta dst_lo
        lda #>wave_col
        sta dst_hi

        ldx #2
dc_pg:  ldy #0
dc_lp:  lda (src_lo),y
        jsr char_to_color
        sta (dst_lo),y
        iny
        bne dc_lp
        inc src_hi
        inc dst_hi
        dex
        bne dc_pg

        ldy #0
dc_rm:  lda (src_lo),y
        jsr char_to_color
        sta (dst_lo),y
        iny
        cpy #168
        bne dc_rm
        rts

char_to_color:
        cmp #$20
        beq ctc_black
        cmp #$2e
        beq ctc_dkgray
        cmp #$3a
        beq ctc_mdgray
        cmp #$2a
        beq ctc_ltgray
        lda #1
        rts
ctc_black:
        lda #0
        rts
ctc_dkgray:
        lda #11
        rts
ctc_mdgray:
        lda #12
        rts
ctc_ltgray: lda #15
        rts

; ---------------------------------------------------------------------------
; density_colors_fire (fire palette)
; ---------------------------------------------------------------------------

density_colors_fire:
        lda #<wave_scr
        sta src_lo
        lda #>wave_scr
        sta src_hi
        lda #<wave_col
        sta dst_lo
        lda #>wave_col
        sta dst_hi

        ldx #2
df_pg:  ldy #0
df_lp:  lda (src_lo),y
        jsr char_to_color_fire
        sta (dst_lo),y
        iny
        bne df_lp
        inc src_hi
        inc dst_hi
        dex
        bne df_pg

        ldy #0
df_rm:  lda (src_lo),y
        jsr char_to_color_fire
        sta (dst_lo),y
        iny
        cpy #168
        bne df_rm
        rts

char_to_color_fire:
        cmp #$20
        beq cfire_black
        cmp #$2e
        beq cfire_brown
        cmp #$3a
        beq cfire_ltred
        cmp #$2a
        beq cfire_orange
        lda #2
        rts
cfire_black:
        lda #0
        rts
cfire_brown:
        lda #9
        rts
cfire_ltred:
        lda #10
        rts
cfire_orange: lda #8
        rts

; ---------------------------------------------------------------------------
; Rainbow table
; ---------------------------------------------------------------------------

rainbow_table:
        .byte  2,  2,  8,  8,  7,  7,  7,  7
        .byte  5,  5,  5,  5, 13, 13, 14, 14
        .byte  6,  6,  6,  6,  4,  4,  4,  4
        .byte 10, 10,  2,  2,  8,  8,  7,  7
        .byte  5,  5, 13, 13, 14, 14,  6,  6
