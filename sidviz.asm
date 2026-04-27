; sidviz_exp.asm
; 64tass assembler
; autostart SYS 2064
;
; version 1.7.9 (2026-04-27)
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
;   $C003     = quit_flag       (Python writes 1 → graceful SID stop + BASIC reset)
;   $C004     = viz_mode        (Python writes: 0=c64audio rows 8-24, 1=extended rows 2-24)
;   $C005     = white_ctable    (128 bytes, screen_code → C64 color, white mode)
;   $C085     = fire_ctable     (128 bytes, screen_code → C64 color, fire mode)
;   $C105     = frame buffer    (680 bytes c64audio / 920 bytes extended)
;   $C500     = ticker buffer   (up to 253 PETSCII chars)
;   $C5FC     = color_mode      (ASM owns: 0=rainbow, 1=white, 2=fire)
;   $C5FD     = irq_tick        (ASM owns)
;   $C5FE     = ticker length   (Python writes)
;   $C5FF     = ticker position (ASM owns)
;   $C600     = JMP initAddress trampoline (Python writes when c64_audio)
;   $C610     = JMP playAddress trampoline (Python writes when c64_audio)
;   $C620/$C621 = SID play vector saved post-init (play_addr=0 SIDs)
;
; Screen layout:
;   Row  0  ($0400) = ticker (always)
;   Row  1  ($0428) = blank spacing row (always)
;   Rows 2-24 ($0450) = visualization (extended/non-c64audio mode)
;   Rows 8-24 ($0540) = visualization (c64audio mode — avoids SID driver zone)
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
ctc_x       = $f7       ; scratch: preserves X across char_to_color calls

; ---------------------------------------------------------------------------
; Hardware
; ---------------------------------------------------------------------------

screen      = $0400
color       = $d800
border      = $d020
bgcol       = $d021
irq_vec_lo  = $0314
irq_vec_hi  = $0315

ticker_scr  = $0400     ; row 0
ticker_col  = $d800     ; row 0 color RAM

; ---------------------------------------------------------------------------
; Python comms
; ---------------------------------------------------------------------------

frame_flag      = $c000
color_flag      = $c001
c64_audio_flag  = $c002   ; 0=off, 1=wait for SID upload, 2=SID ready
quit_flag       = $c003   ; Python writes 1 to trigger graceful SID stop + BASIC reset
viz_mode        = $c004   ; 0=c64audio (rows 8-24), 1=extended (rows 2-24)
white_ctable    = $c005   ; 128-byte color table, screen_code → C64 color (white mode)
fire_ctable     = $c085   ; 128-byte color table, screen_code → C64 color (fire mode)
frame_buf       = $c105   ; 680 or 920 bytes depending on viz_mode
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

        ; Clear comms flags — Python must re-write $C002=$01 after run_prg
        ; if C64 audio wanted.  viz_mode ($C004) is intentionally NOT cleared
        ; here: Python writes it before the reboot so init uses the right value.
        sta frame_flag
        sta color_flag
        sta color_mode
        sta ticker_pos
        sta c64_audio_flag
        sta quit_flag

        ; Init IRQ tick counter
        lda #SCROLL_RATE
        sta irq_tick

        ; Set up display
        jsr fill_color_rainbow
        jsr fill_ticker_color
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
        lda #<irq_handler
        sta irq_vec_lo
        lda #>irq_handler
        sta irq_vec_hi

        cli

; ---------------------------------------------------------------------------
; Main loop
; ---------------------------------------------------------------------------

main_loop:
        lda quit_flag
        bne do_quit
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
; do_quit: silence SID and return to BASIC
; Called from main loop when quit_flag ($C003) is set by Python.
; SEI prevents IRQ from re-triggering SID play while we zero the chip.
; JMP $FCE2 = C64 power-on cold start → clears screen, shows BASIC READY.
; ---------------------------------------------------------------------------

do_quit:
        sei
        ldx #24
dq_sil: lda #$00
        sta $d400,x
        dex
        bpl dq_sil
        jmp $fce2

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
; copy_frame: frame_buf ($C105) -> screen RAM
;   viz_mode=0 (c64audio):  680 bytes -> $0540 (rows  8-24, 2 pages + 168)
;   viz_mode=1 (extended):  920 bytes -> $0450 (rows  2-24, 3 pages + 152)
; ---------------------------------------------------------------------------

copy_frame:
        lda #<frame_buf
        sta src_lo
        lda #>frame_buf
        sta src_hi

        lda viz_mode
        bne cf_ext

        ; c64audio: 680 bytes -> $0540 (2 pages + 168)
        lda #<$0540
        sta dst_lo
        lda #>$0540
        sta dst_hi
        ldx #2
cf_c_pg:ldy #0
cf_c_lp:lda (src_lo),y
        sta (dst_lo),y
        iny
        bne cf_c_lp
        inc src_hi
        inc dst_hi
        dex
        bne cf_c_pg
        ldy #0
cf_c_rm:lda (src_lo),y
        sta (dst_lo),y
        iny
        cpy #168
        bne cf_c_rm
        jmp cf_done

cf_ext: ; extended: 920 bytes -> $0450 (3 pages + 152)
        lda #<$0450
        sta dst_lo
        lda #>$0450
        sta dst_hi
        ldx #3
cf_e_pg:ldy #0
cf_e_lp:lda (src_lo),y
        sta (dst_lo),y
        iny
        bne cf_e_lp
        inc src_hi
        inc dst_hi
        dex
        bne cf_e_pg
        ldy #0
cf_e_rm:lda (src_lo),y
        sta (dst_lo),y
        iny
        cpy #152
        bne cf_e_rm

cf_done:
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
; fill_color_rainbow: color RAM for visualization rows
;   viz_mode=0: rows 8-24 ($D940, 680 bytes = 2 pages + 168)
;   viz_mode=1: rows 2-24 ($D850, 920 bytes = 3 pages + 152)
; ---------------------------------------------------------------------------

fill_color_rainbow:
        lda viz_mode
        bne fcr_ext

        ; c64audio: $D940, 680 bytes
        lda #<$d940
        sta dst_lo
        lda #>$d940
        sta dst_hi
        ldx #0
        ldy #0
fcr_c0: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_c0n
        ldx #0
fcr_c0n:iny
        bne fcr_c0
        inc dst_hi
        ldy #0
fcr_c1: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_c1n
        ldx #0
fcr_c1n:iny
        bne fcr_c1
        inc dst_hi
        ldy #0
fcr_c_r:lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_crn
        ldx #0
fcr_crn:iny
        cpy #168
        bne fcr_c_r
        rts

fcr_ext:; extended: $D850, 920 bytes
        lda #<$d850
        sta dst_lo
        lda #>$d850
        sta dst_hi
        ldx #0
        ldy #0
fcr_e0: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_e0n
        ldx #0
fcr_e0n:iny
        bne fcr_e0
        inc dst_hi
        ldy #0
fcr_e1: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_e1n
        ldx #0
fcr_e1n:iny
        bne fcr_e1
        inc dst_hi
        ldy #0
fcr_e2: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_e2n
        ldx #0
fcr_e2n:iny
        bne fcr_e2
        inc dst_hi
        ldy #0
fcr_e_r:lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_ern
        ldx #0
fcr_ern:iny
        cpy #152
        bne fcr_e_r
        rts

; ---------------------------------------------------------------------------
; fill_color_white: color RAM for visualization rows, all white (1)
;   viz_mode=0: rows 8-24 ($D940, 680 bytes = 2 pages + 168)
;   viz_mode=1: rows 2-24 ($D850, 920 bytes = 3 pages + 152)
; ---------------------------------------------------------------------------

fill_color_white:
        lda viz_mode
        bne fcw_ext
        ; c64audio: $D940, 680 bytes (2 pages + 168)
        lda #<$d940
        sta dst_lo
        lda #>$d940
        sta dst_hi
        lda #1
        ldx #2
fcw_c_p:ldy #0
fcw_c_l:sta (dst_lo),y
        iny
        bne fcw_c_l
        inc dst_hi
        dex
        bne fcw_c_p
        ldy #0
fcw_c_r:sta (dst_lo),y
        iny
        cpy #168
        bne fcw_c_r
        rts

fcw_ext:; extended: $D850, 920 bytes (3 pages + 152)
        lda #<$d850
        sta dst_lo
        lda #>$d850
        sta dst_hi
        lda #1
        ldx #3
fcw_e_p:ldy #0
fcw_e_l:sta (dst_lo),y
        iny
        bne fcw_e_l
        inc dst_hi
        dex
        bne fcw_e_p
        ldy #0
fcw_e_r:sta (dst_lo),y
        iny
        cpy #152
        bne fcw_e_r
        rts

; ---------------------------------------------------------------------------
; fill_ticker_color: row 0 ($D800, 40 bytes) color 13 (light green)
; ---------------------------------------------------------------------------

fill_ticker_color:
        lda #<color
        sta dst_lo
        lda #>color
        sta dst_hi
        lda #13
        ldy #0
ftc_lp: sta (dst_lo),y
        iny
        cpy #40
        bne ftc_lp
        rts

; ---------------------------------------------------------------------------
; density_colors: white density palette — lookup via white_ctable ($C005)
;   viz_mode=0: src=$0540, dst=$D940 (680 bytes = 2 pages + 168)
;   viz_mode=1: src=$0450, dst=$D850 (920 bytes = 3 pages + 152)
; ---------------------------------------------------------------------------

density_colors:
        lda viz_mode
        bne dc_ext
        ; c64audio: src=$0540, dst=$D940, 680 bytes (2 pages + 168)
        lda #<$0540
        sta src_lo
        lda #>$0540
        sta src_hi
        lda #<$d940
        sta dst_lo
        lda #>$d940
        sta dst_hi
        ldx #2
dc_c_pg:ldy #0
dc_c_lp:lda (src_lo),y
        jsr char_to_color
        sta (dst_lo),y
        iny
        bne dc_c_lp
        inc src_hi
        inc dst_hi
        dex
        bne dc_c_pg
        ldy #0
dc_c_rm:lda (src_lo),y
        jsr char_to_color
        sta (dst_lo),y
        iny
        cpy #168
        bne dc_c_rm
        rts

dc_ext: ; extended: src=$0450, dst=$D850, 920 bytes (3 pages + 152)
        lda #<$0450
        sta src_lo
        lda #>$0450
        sta src_hi
        lda #<$d850
        sta dst_lo
        lda #>$d850
        sta dst_hi
        ldx #3
dc_e_pg:ldy #0
dc_e_lp:lda (src_lo),y
        jsr char_to_color
        sta (dst_lo),y
        iny
        bne dc_e_lp
        inc src_hi
        inc dst_hi
        dex
        bne dc_e_pg
        ldy #0
dc_e_rm:lda (src_lo),y
        jsr char_to_color
        sta (dst_lo),y
        iny
        cpy #152
        bne dc_e_rm
        rts

char_to_color:
        stx ctc_x
        tax
        lda white_ctable,x
        ldx ctc_x
        rts

; ---------------------------------------------------------------------------
; density_colors_fire: fire palette — lookup via fire_ctable ($C085)
;   viz_mode=0: src=$0540, dst=$D940 (680 bytes = 2 pages + 168)
;   viz_mode=1: src=$0450, dst=$D850 (920 bytes = 3 pages + 152)
; ---------------------------------------------------------------------------

density_colors_fire:
        lda viz_mode
        bne df_ext
        ; c64audio: src=$0540, dst=$D940, 680 bytes (2 pages + 168)
        lda #<$0540
        sta src_lo
        lda #>$0540
        sta src_hi
        lda #<$d940
        sta dst_lo
        lda #>$d940
        sta dst_hi
        ldx #2
df_c_pg:ldy #0
df_c_lp:lda (src_lo),y
        jsr char_to_color_fire
        sta (dst_lo),y
        iny
        bne df_c_lp
        inc src_hi
        inc dst_hi
        dex
        bne df_c_pg
        ldy #0
df_c_rm:lda (src_lo),y
        jsr char_to_color_fire
        sta (dst_lo),y
        iny
        cpy #168
        bne df_c_rm
        rts

df_ext: ; extended: src=$0450, dst=$D850, 920 bytes (3 pages + 152)
        lda #<$0450
        sta src_lo
        lda #>$0450
        sta src_hi
        lda #<$d850
        sta dst_lo
        lda #>$d850
        sta dst_hi
        ldx #3
df_e_pg:ldy #0
df_e_lp:lda (src_lo),y
        jsr char_to_color_fire
        sta (dst_lo),y
        iny
        bne df_e_lp
        inc src_hi
        inc dst_hi
        dex
        bne df_e_pg
        ldy #0
df_e_rm:lda (src_lo),y
        jsr char_to_color_fire
        sta (dst_lo),y
        iny
        cpy #152
        bne df_e_rm
        rts

char_to_color_fire:
        stx ctc_x
        tax
        lda fire_ctable,x
        ldx ctc_x
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
