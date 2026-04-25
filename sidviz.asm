; sidviz_exp.asm
; 64tass assembler
; autostart SYS 2064
;
; version 1.6.7b (2026-04-25)
;
; Single PRG handles all modes via $C002 flag:
;   $C002 = $00  Mac/MP3 mode   — no SID player, normal display
;   $C002 = $01  C64 audio      — Python is uploading SID, wait
;   $C002 = $02  C64 audio      — SID uploaded, call init + enable play
;
; Memory map:
;   $C000     = frame flag      (Python writes 1, ASM clears to 0)
;   $C001     = color flag      (2=rainbow, 1=white density, 3=fire density)
;   $C002     = c64_audio_flag  (0=off, 1=wait, 2=ready — Python controls)
;   $C100     = frame buffer    (680 bytes, rows 8-24)
;   $C500     = ticker buffer   (up to 253 PETSCII chars)
;   $C5FC     = color_mode      (ASM owns: 0=rainbow, 1=white, 2=fire)
;   $C5FD     = irq_tick        (ASM owns)
;   $C5FE     = ticker length   (Python writes)
;   $C5FF     = ticker position (ASM owns)
;   $C600     = JMP initAddress trampoline (Python writes when c64_audio)
;   $C610     = JMP playAddress trampoline (Python writes when c64_audio)
;   $C620/$C621 = SID play vector saved post-init (play_addr=0 SIDs)
;
; ZP usage:
;   $F9/$FA   = saved original IRQ vector (for chain)
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
wave_scr    = $0540     ; row 8 — above SID driver zone $0400-$04FF
wave_col    = $d940

; ---------------------------------------------------------------------------
; Python comms
; ---------------------------------------------------------------------------

frame_flag      = $c000
color_flag      = $c001
c64_audio_flag  = $c002   ; 0=off, 1=wait for SID upload, 2=SID ready
frame_buf       = $c100
ticker_buf      = $c500
color_mode      = $c5fc
irq_tick        = $c5fd
ticker_len      = $c5fe
ticker_pos      = $c5ff

SCROLL_RATE = 6

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

        ; Clear ALL comms flags including c64_audio_flag
        ; Python must re-write $C002=$01 after run_prg if C64 audio wanted
        sta frame_flag
        sta color_flag
        sta color_mode
        sta ticker_pos
        sta c64_audio_flag

        ; Init IRQ tick counter
        lda #SCROLL_RATE
        sta irq_tick

        ; Set up display
        jsr fill_color_rainbow
        jsr fill_ticker_color
        jsr fill_row0_color
        jsr clear_screen

        ; Pre-fill ticker buffer with spaces until Python sends real content
        lda #$20
        ldy #0
ptb_lp: sta ticker_buf,y
        iny
        bne ptb_lp
        lda #40
        sta ticker_len

        ; Save clean KERNAL IRQ vector BEFORE any SID init can hook it
        lda irq_vec_lo
        sta orig_irq_lo
        lda irq_vec_hi
        sta orig_irq_hi

        ; Check if C64 audio mode requested
        lda c64_audio_flag
        beq no_c64_audio    ; $00 = Mac/MP3 mode, skip SID setup

        ; C64 audio mode: wait for Python to upload SID ($C002 -> $02)
wait_sid:
        lda c64_audio_flag
        cmp #$02
        bne wait_sid

        ; Call SID INIT via trampoline at $C600
        ; Python wrote: JMP initAddress at $C600
        ; JSR $C600 -> JMP initAddr -> init runs -> RTS returns here
        jsr $c600

        ; Save SID's post-init IRQ vector ($0314/$0315) to $C620/$C621 before
        ; we overwrite $0314 with irq_handler.  Python reads $C620/$C621 for
        ; play_addr=0 SIDs to get the correct play routine address.
        lda irq_vec_lo
        sta $c620
        lda irq_vec_hi
        sta $c621

no_c64_audio:
        ; Hook IRQ to our handler
        ; If C64 audio: orig_irq_lo/hi has pre-SID KERNAL vector (safe chain)
        ; If Mac/MP3:   orig_irq_lo/hi has KERNAL vector (normal chain)
        lda #<irq_handler
        sta irq_vec_lo
        lda #>irq_handler
        sta irq_vec_hi

        cli

; ---------------------------------------------------------------------------
; Main loop
; ---------------------------------------------------------------------------

main_loop:
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
; IRQ handler — saves/restores all registers
; Calls SID play directly at IRQ rate, scrolls ticker
; ---------------------------------------------------------------------------

irq_handler:
        pha
        txa
        pha
        tya
        pha

        ; Call SID play directly — exact timing regardless of main loop load.
        ; Save/restore ZP pointers $F9-$FE: SID play routines may clobber them,
        ; corrupting copy_frame's src/dst pointers or the IRQ chain pointer.
        lda c64_audio_flag
        cmp #$02
        bne irq_skip_play
        lda $f9
        pha
        lda $fa
        pha
        lda $fb
        pha
        lda $fc
        pha
        lda $fd
        pha
        lda $fe
        pha
        jsr $c610
        pla
        sta $fe
        pla
        sta $fd
        pla
        sta $fc
        pla
        sta $fb
        pla
        sta $fa
        pla
        sta $f9
irq_skip_play:

        ; Ticker scroll every SCROLL_RATE IRQs
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
        jmp (orig_irq_lo)

; ---------------------------------------------------------------------------
; copy_frame: $C100 -> $0540 (680 bytes = 17 rows x 40 cols)
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

        ; 2 full pages = 512 bytes
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

        ; Remaining 168 bytes (680 - 512)
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
; clear_screen: fill $0400-$07E7 (1000 bytes) with space
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
; fill_color_rainbow: color RAM rows 8-24 ($D940-$DBE7, 680 bytes)
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
; fill_color_white: color RAM rows 8-24, all white (1)
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
; fill_ticker_color: row 1 ($D828, 40 bytes) color 13 (light green)
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
; fill_row0_color: row 0 ($D800, 40 bytes) color 0 (black)
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
; density_colors: white density palette
; space->0, .->11, :->12, *->15, #/@->1
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
ctc_ltgray:
        lda #15
        rts

; ---------------------------------------------------------------------------
; density_colors_fire: fire palette
; space->0, .->9(brown), :->10(ltred), *->8(orange), #/@->2(red)
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
cfire_orange:
        lda #8
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
