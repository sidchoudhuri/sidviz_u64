; sidviz_exp.asm
; 64tass assembler
; autostart SYS 2064
;
; version 1.9.9 (2026-11-18)
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
;   $C004     = bg_flag         (Python writes 1 once a background picture is uploaded)
;   $C005     = bg_wcol         (0=waveform keeps the colour mode, 1=waveform takes the picture's colour)
;   $C006     = bg_colx         (ASM owns: column counter for copy_frame_bg)
;   $C007-$C020 = bitmap background (see "Bitmap background" below)
;   $C100     = frame buffer    (680 bytes, rows 8-24)
;   $C500     = ticker buffer   (up to 253 PETSCII chars)
;   $C5FC     = color_mode      (ASM owns: 0=rainbow, 1=white, 2=fire)
;   $C5FD     = irq_tick        (ASM owns)
;   $C5FE     = ticker length   (Python writes)
;   $C5FF     = ticker position (ASM owns)
;   $C600     = JMP initAddress trampoline (Python writes when c64_audio)
;   $C610     = JMP playAddress trampoline (Python writes when c64_audio)
;   $C620/$C621 = SID play vector saved post-init (play_addr=0 SIDs)
;   $C700     = background picture, screen codes (680 bytes, rows 8-24)
;   $CA00     = background picture, colours      (680 bytes, rows 8-24)
;
; Bitmap background (Koala or hires), VIC bank 2:
;   $8000     = text screen (ticker rows, and the visualizer rows in a top/bottom split)
;   $8400     = bitmap colours (screen matrix)          $9000 = character ROM (as the VIC sees it)
;   $A000     = bitmap, under BASIC ROM                 $E000 = clean copy of the bitmap, under KERNAL ROM
;   $9000-$9BFF (CPU RAM) = clean matrix, clean colour RAM, cells drawn last frame
;   $9C00     = waveform glyphs (32 x 8)   $9D00 = screen code -> glyph   $9E00 = glyph codes (Python)
;   $C700     = visualizer frame (up to 920 bytes)
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
quit_flag       = $c003   ; Python writes 1 to trigger graceful SID stop + BASIC reset
bg_flag         = $c004   ; Python writes 1 once the background picture is uploaded
bg_wcol         = $c005   ; 0=waveform keeps the colour mode, 1=waveform takes the picture's colour
bg_colx         = $c006   ; column counter for copy_frame_bg (rainbow colours)

; Bitmap background
bm_mode         = $c007   ; ASM: 0 = text, 1 = waveform drawn into the bitmap, 2 = top/bottom split, 3 = stopped
bm_cmd          = $c008   ; Python writes 1 once the picture and the settings are uploaded
bm_stop         = $c009   ; Python writes 1 at the end (local audio): raster split off, whole screen bitmap
bm_line         = $c00a   ; raster line where the bitmap starts
tx_line         = $c00b   ; raster line where text mode starts again
bm_d016         = $c00c   ; $d8 = Koala (multicolour), $c8 = hires
bm_koala        = $c00d   ; 1 = Koala, 0 = hires
vr0             = $c00e   ; visualizer area: first row
vrn             = $c00f   ;                  rows
vc0             = $c010   ;                  first column
vcn             = $c011   ;                  columns
bm_bg           = $c012   ; Koala background colour ($d021 in the bitmap part)
bm_kind         = $c013   ; Python: 1 = drawn into the bitmap, 2 = top/bottom split
rs_phase        = $c014   ; ASM: 0 = next raster IRQ starts the bitmap, 1 = starts text
bm_row          = $c015   ; ASM scratch from here
bm_left         = $c016
bm_cend         = $c017
cur_char        = $c018
bm_tmp          = $c019
bp_lo           = $c01a   ; bitmap address of the current cell
bp_hi           = $c01b
fp_lo           = $c01c   ; frame address of the current row
fp_hi           = $c01d
gl_count        = $c01e   ; Python: number of glyph codes at $9e00
bm_fin          = $c01f
bm_fout         = $c020
bm_frame        = $c700
clean_mx        = $9000
clean_co        = $9400
prev_ch         = $9800
glyphs          = $9c00
glyph_map       = $9d00
glyph_codes     = $9e00
frame_buf       = $c100   ; 680 bytes ($c100-$c3a7)
white_ctable    = $c3a8   ; 128-byte color table, screen_code → C64 color (white mode)
fire_ctable     = $c428   ; 128-byte color table, screen_code → C64 color (fire mode)
ticker_buf      = $c500
bg_chars        = $c700   ; 680 bytes ($c700-$c9a7): background screen codes, rows 8-24
bg_cols         = $ca00   ; 680 bytes ($ca00-$cca7): background colours, rows 8-24
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

        ; Copy the bitmap background code to $8800 (see the end of this file)
        lda #<bm_image
        sta bc_src+1
        lda #>bm_image
        sta bc_src+2
        ldy #>(bm_image_end-bm_image+255)
bc_pg:  ldx #0
bc_src: lda $ffff,x
bc_dst: sta $8800,x
        inx
        bne bc_src
        inc bc_src+2
        inc bc_dst+2
        dey
        bne bc_pg

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
        sta quit_flag
        sta bg_flag
        sta bm_mode
        sta bm_cmd
        sta bm_stop

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
        lda quit_flag
        bne do_quit
        lda bm_cmd
        beq ml_nocmd
        jsr bm_start
ml_nocmd:
        lda bm_stop
        beq ml_nostop
        jsr bm_halt
ml_nostop:
        lda bm_mode
        beq ml_text
        jmp bm_loop
ml_text:
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
        cmp #$03
        bne ml_manual
        lda #$02
        sta color_mode
        jmp clr_cflag
ml_manual:
        lda #$03
        sta color_mode

clr_cflag:
        lda #$00
        sta color_flag

check_frame:
        lda frame_flag
        beq main_loop
        lda bg_flag
        beq cf_plain
        jsr copy_frame_bg       ; background picture: chars and colours in one pass
        jmp main_loop
cf_plain:
        jsr copy_frame
        lda color_mode
        beq main_loop
        cmp #$01
        beq do_density_white
        cmp #$03
        beq main_loop
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
        lda #0
        sta $d01a               ; raster IRQ off (bitmap background)
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
        lda bm_mode             ; bitmap background: raster split as well as the CIA timer
        beq irq_text
        cmp #3
        beq irq_text
        lda $d019
        bpl irq_cia
        and #$01
        beq irq_cia
        sta $d019               ; acknowledge the raster IRQ
        jmp rs_irq
irq_cia:
        lda $dc0d               ; acknowledge the CIA now, so the raster split can interrupt the
        cli                     ; SID player and the ticker (it has to run on time)
irq_text:
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
tk1     lda ticker_scr+1,x      ; tk1/tk2/tk3: moved to $80xx for a bitmap background
tk2     sta ticker_scr,x
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
tk3     sta ticker_scr+39

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
; copy_frame_bg: as copy_frame, with a background picture behind the waveform.
; For each of the 680 cells (rows 8-24):
;   frame byte = space -> the picture's screen code and colour (bg_chars / bg_cols)
;   anything else      -> the frame byte, coloured by the colour mode (rainbow by
;                         column, white/fire by the density tables), or with the
;                         picture's colour when bg_wcol is set
; ---------------------------------------------------------------------------

bg_seg  .macro                  ; \1 = offset into the buffers, \2 = cells (0 = 256)
        ldy #0
lp      lda frame_buf+\1,y
        cmp #$20
        beq pic
        sta wave_scr+\1,y
        ldx bg_wcol
        bne piccol
        ldx color_mode
        beq rb
        and #$7f
        cpx #$01
        beq wh
        tax
        lda fire_ctable,x
        jmp setc
wh      tax
        lda white_ctable,x
        jmp setc
rb      ldx bg_colx
        lda rainbow_table,x
        jmp setc
pic     lda bg_chars+\1,y
        sta wave_scr+\1,y
piccol  lda bg_cols+\1,y
setc    sta wave_col+\1,y
        ldx bg_colx
        inx
        cpx #40
        bcc nowrap
        ldx #0
nowrap  stx bg_colx
        iny
        cpy #\2
        bne lp
        .endm

copy_frame_bg:
        lda #0
        sta bg_colx
        #bg_seg 0, 0
        #bg_seg 256, 0
        #bg_seg 512, 168
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
; density_colors: white density palette — colors set by Python via white_ctable ($C3A8)
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
        stx ctc_x
        and #$7f            ; 128-byte table: codes 128+ share a slot (160 -> space's)
        tax
        lda white_ctable,x
        ldx ctc_x
        rts

; ---------------------------------------------------------------------------
; density_colors_fire: fire palette — colors set by Python via fire_ctable ($C428)
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
        stx ctc_x
        and #$7f            ; 128-byte table: codes 128+ share a slot (160 -> space's)
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

; ===========================================================================
; The bitmap background code runs at $8800 (RAM the VIC doesn't show in bank 2).
; It is stored here and copied to $8800 when the program starts, so the part
; that stays at $0810 ends below $1000, where many tunes load.
; ===========================================================================

bm_image:
        .logical $8800

; Bitmap background (Koala or hires) — VIC bank 2, raster split for the text rows

; bm_loop: the main loop while a bitmap background is up
bm_loop:
        cmp #3
        beq bm_idle
        lda color_flag
        beq bml_frame
        cmp #$02
        bne bml_nr
        lda #0
        beq bml_setc
bml_nr: cmp #$01
        bne bml_fire
        lda #1
        bne bml_setc
bml_fire:
        lda #2
bml_setc:
        sta color_mode
        lda #0
        sta color_flag
        lda bm_mode
        cmp #1
        bne bml_frame
        jsr bm_force            ; new colours: redraw every cell next frame
bml_frame:
        lda frame_flag
        beq bml_done
        lda bm_mode
        cmp #1
        bne bml_text
        jsr bm_draw
        jmp main_loop
bml_text:
        jsr bm_text
bml_done:
        jmp main_loop
bm_idle:
        lda #0
        sta frame_flag
        sta color_flag
        jmp main_loop

; rs_irq: the raster split. Entered through the KERNAL (A, X, Y saved); leaves through $ea81.
; The IRQ comes 4 lines early and waits, so a late start (another IRQ running) still switches on time.
rs_irq:
        lda rs_phase
        bne rs_text
rs_w1:  lda $d012
        cmp bm_line
        bcc rs_w1
        lda #$3b                ; bitmap on
        sta $d011
        lda bm_d016
        sta $d016
        lda #$18                ; matrix $8400, bitmap $a000
        sta $d018
        lda bm_bg
        sta $d021
        lda #1
        sta rs_phase
        lda tx_line
        sec
        sbc #4
        sta $d012
        jmp $ea81
rs_text:
rs_w2:  lda $d012
        cmp tx_line
        bcc rs_w2
        lda #$1b                ; text on
        sta $d011
        lda #$c8
        sta $d016
        lda #$04                ; screen $8000, characters $9000 (ROM)
        sta $d018
        lda #0
        sta $d021
        sta rs_phase
        lda bm_line
        sec
        sbc #4
        sta $d012
        jmp $ea81

; bm_start: Python has uploaded the picture ($a000, $8400, colour RAM) and the settings
bm_start:
        lda #0
        sta bm_cmd
        lda #$36                ; BASIC ROM out, so the CPU reads the bitmap at $a000
        sta $01
        lda #$a0                ; clean copy: $a000-$bf3f -> $e000-$ff3f
        sta bs_src+2
        lda #$e0
        sta bs_dst+2
        ldy #31
bs_pg:  ldx #0
bs_src: lda $a000,x
bs_dst: sta $e000,x
        inx
        bne bs_src
        inc bs_src+2
        inc bs_dst+2
        dey
        bne bs_pg
        ldx #0
bs_tl:  lda $bf00,x
        sta $ff00,x
        inx
        cpx #$40
        bne bs_tl
        ldx #0                  ; clean matrix and colours, last-frame cells, text screen
bs_l2:  lda $8400,x
        sta clean_mx,x
        lda $8500,x
        sta clean_mx+$100,x
        lda $8600,x
        sta clean_mx+$200,x
        lda $8700,x
        sta clean_mx+$300,x
        lda $d800,x
        and #$0f
        sta clean_co,x
        lda $d900,x
        and #$0f
        sta clean_co+$100,x
        lda $da00,x
        and #$0f
        sta clean_co+$200,x
        lda $db00,x
        and #$0f
        sta clean_co+$300,x
        lda #$20
        sta prev_ch,x
        sta prev_ch+$100,x
        sta prev_ch+$200,x
        sta prev_ch+$300,x
        sta $8000,x
        sta $8100,x
        sta $8200,x
        sta $8300,x
        inx
        bne bs_l2
        lda #<bm_nmi            ; an NMI while the KERNAL is switched out lands on an RTI
        sta $fffa
        lda #>bm_nmi
        sta $fffb
        jsr bm_glyphs
        lda #$80                ; the ticker moves to the text screen at $8000
        sta tk1+2
        sta tk2+2
        sta tk3+2
        sei
        lda $dd00               ; VIC bank 2 ($8000-$bfff)
        and #$fc
        ora #$01
        sta $dd00
        lda #$1b
        sta $d011
        lda #$c8
        sta $d016
        lda #$04
        sta $d018
        lda #0
        sta rs_phase
        lda bm_line
        sec
        sbc #4
        sta $d012
        lda #$0f
        sta $d019
        lda #$01
        sta $d01a               ; raster IRQ on
        lda bm_kind
        sta bm_mode
        cli
        rts
bm_nmi: rti

; bm_glyphs: the waveform characters, from the character ROM, made fat for Koala
bm_glyphs:
        lda gl_count
        beq bg_done
        sei
        lda #$33                ; character ROM in at $d000
        sta $01
        ldx #0
bg_lp:  stx bm_tmp
        lda glyph_codes,x
        sta cur_char
        and #$7f
        pha
        lsr
        lsr
        lsr
        lsr
        lsr
        clc
        adc #$d0
        sta bg_rd+2
        pla
        asl
        asl
        asl
        sta bg_rd+1
        lda bm_tmp
        asl
        asl
        asl
        sta bg_wr+1
        ldy #0
bg_rd:  lda $d000,y
        bit cur_char
        bpl bg_noinv
        eor #$ff                ; codes 128-255 are the reversed characters
bg_noinv:
        ldx bm_koala
        beq bg_wr
        jsr fatten
bg_wr:  sta glyphs,y
        iny
        cpy #8
        bne bg_rd
        ldx bm_tmp
        inx
        cpx gl_count
        bne bg_lp
        lda #$36
        sta $01
        cli
bg_done:
        rts

; fatten: each pair of pixels with either pixel set becomes %11 (Koala's colour RAM colour)
fatten: sta bm_fin
        lda #0
        sta bm_fout
        lda bm_fin
        and #$c0
        beq ft1
        lda #$c0
        ora bm_fout
        sta bm_fout
ft1:    lda bm_fin
        and #$30
        beq ft2
        lda #$30
        ora bm_fout
        sta bm_fout
ft2:    lda bm_fin
        and #$0c
        beq ft3
        lda #$0c
        ora bm_fout
        sta bm_fout
ft3:    lda bm_fin
        and #$03
        beq ft4
        lda #$03
        ora bm_fout
        sta bm_fout
ft4:    lda bm_fout
        rts

; bm_halt: end of a local-audio run — leave the picture on screen, whole screen bitmap
bm_halt:
        sei
        lda #0
        sta bm_stop
        sta $d01a
        lda #$0f
        sta $d019
        lda #$3b
        sta $d011
        lda bm_d016
        sta $d016
        lda #$18
        sta $d018
        lda bm_bg
        sta $d021
        lda #3
        sta bm_mode
        lda #$37
        sta $01
        cli
        rts

; bm_force: forget what was drawn, so every cell is redrawn
bm_force:
        lda #$ff
        ldx #0
bf_lp:  sta prev_ch,x
        sta prev_ch+$100,x
        sta prev_ch+$200,x
        sta prev_ch+$300,x
        inx
        bne bf_lp
        rts

; bm_draw: draw the frame at $c700 into the bitmap, over the visualizer area.
; A cell that changed since the last frame is restored from the clean copy,
; then (unless it is a space) gets its glyph ORed in and the visualizer colour.
bm_draw:
        lda #$36
        sta $01
        sec
        lda #<bm_frame
        sbc vc0
        sta fp_lo
        lda #>bm_frame
        sbc #0
        sta fp_hi
        lda vr0
        sta bm_row
        lda vrn
        sta bm_left
        clc
        lda vc0
        adc vcn
        sta bm_cend
bd_row: ldx bm_row
        lda fp_lo
        sta pd_fr+1
        lda fp_hi
        sta pd_fr+2
        lda row40_lo,x          ; every per-cell table starts on a page: same low byte
        sta pd_pv1+1
        sta pd_pv2+1
        sta pd_mx1+1
        sta pd_mx2+1
        sta pd_cm1+1
        sta pd_cm2+1
        sta pd_co1+1
        sta pd_co2+1
        sta pd_cc1+1
        lda row40_hi,x
        clc
        adc #>prev_ch
        sta pd_pv1+2
        sta pd_pv2+2
        lda row40_hi,x
        clc
        adc #$84
        sta pd_mx1+2
        sta pd_mx2+2
        lda row40_hi,x
        clc
        adc #>clean_mx
        sta pd_cm1+2
        sta pd_cm2+2
        lda row40_hi,x
        clc
        adc #$d8
        sta pd_co1+2
        sta pd_co2+2
        lda row40_hi,x
        clc
        adc #>clean_co
        sta pd_cc1+2
        lda #0                  ; bp = $a000 + row*320 + vc0*8
        sta bp_hi
        lda vc0
        asl
        rol bp_hi
        asl
        rol bp_hi
        asl
        rol bp_hi
        clc
        adc row320_lo,x
        sta bp_lo
        lda bp_hi
        adc row320_hi,x
        sta bp_hi
        ldy vc0
bd_col:
pd_fr:  lda $ffff,y
pd_pv1: cmp $ffff,y
        beq bd_next
pd_pv2: sta $ffff,y
        sta cur_char
        jsr bm_restore
        lda cur_char
        cmp #$20
        beq bd_next
        jsr bm_glyph
bd_next:
        clc
        lda bp_lo
        adc #8
        sta bp_lo
        bcc bd_nc
        inc bp_hi
bd_nc:  iny
        cpy bm_cend
        bne bd_col
        clc
        lda fp_lo
        adc vcn
        sta fp_lo
        bcc bd_nc2
        inc fp_hi
bd_nc2: inc bm_row
        dec bm_left
        beq bd_end
        jmp bd_row
bd_end: lda #0
        sta frame_flag
        rts

; bm_restore: the cell at bp (column Y) back to the clean picture
bm_restore:
        lda bp_lo
        sta pr_src+1
        sta pr_dst+1
        lda bp_hi
        sta pr_dst+2
        clc
        adc #$40                ; $a000 -> $e000
        sta pr_src+2
        sei
        lda #$35                ; KERNAL out for the clean copy (IRQs off meanwhile: 8 bytes)
        sta $01
        ldx #7
pr_src: lda $ffff,x
pr_dst: sta $ffff,x
        dex
        bpl pr_src
        lda #$36
        sta $01
        cli
pd_cm1: lda $ffff,y
pd_mx1: sta $ffff,y
pd_cc1: lda $ffff,y
pd_co1: sta $ffff,y
        rts

; bm_glyph: OR the glyph for cur_char into the cell at bp, and colour it
bm_glyph:
        ldx cur_char
        lda glyph_map,x
        sta pg_g+1
        lda bp_lo
        sta pg_r+1
        sta pg_w+1
        lda bp_hi
        sta pg_r+2
        sta pg_w+2
        ldx #7
pg_r:   lda $ffff,x
pg_g:   ora glyphs,x
pg_w:   sta $ffff,x
        dex
        bpl pg_r
        lda bg_wcol             ; 1 = keep the picture's colour
        bne pg_done
        ldx color_mode
        beq pg_rb
        lda cur_char
        and #$7f
        tax
        lda color_mode
        cmp #1
        beq pg_wh
        lda fire_ctable,x
        jmp pg_set
pg_wh:  lda white_ctable,x
        jmp pg_set
pg_rb:  lda rainbow_table,y
pg_set: ldx bm_koala
        beq pg_hires
pd_co2: sta $ffff,y             ; Koala: the glyph is %11 pixels = colour RAM
        rts
pg_hires:
        asl                     ; hires: the glyph is set bits = the matrix's high nibble
        asl
        asl
        asl
        sta bm_tmp
pd_cm2: lda $ffff,y
        and #$0f
        ora bm_tmp
pd_mx2: sta $ffff,y
pg_done:
        rts

; bm_text: top/bottom split — the frame at $c700 goes to the text rows at $8000
bm_text:
        lda #<bm_frame
        sta pt_fr+1
        lda #>bm_frame
        sta pt_fr+2
        lda vr0
        sta bm_row
        lda vrn
        sta bm_left
bt_row: ldx bm_row
        lda row40_lo,x
        sta pt_sc+1
        sta pt_co+1
        lda row40_hi,x
        clc
        adc #$80
        sta pt_sc+2
        lda row40_hi,x
        clc
        adc #$d8
        sta pt_co+2
        ldy #0
pt_fr:  lda $ffff,y
pt_sc:  sta $ffff,y
        ldx color_mode
        beq bt_rb
        and #$7f
        tax
        lda color_mode
        cmp #1
        beq bt_wh
        lda fire_ctable,x
        jmp pt_co
bt_wh:  lda white_ctable,x
        jmp pt_co
bt_rb:  lda rainbow_table,y
pt_co:  sta $ffff,y
        iny
        cpy #40
        bne pt_fr
        clc
        lda pt_fr+1
        adc #40
        sta pt_fr+1
        bcc bt_nc
        inc pt_fr+2
bt_nc:  inc bm_row
        dec bm_left
        bne bt_row
        lda #0
        sta frame_flag
        rts

row40_lo  .byte $00,$28,$50,$78,$a0,$c8,$f0,$18,$40,$68,$90,$b8,$e0,$08,$30,$58,$80,$a8,$d0,$f8,$20,$48,$70,$98,$c0
row40_hi  .byte $00,$00,$00,$00,$00,$00,$00,$01,$01,$01,$01,$01,$01,$02,$02,$02,$02,$02,$02,$02,$03,$03,$03,$03,$03
row320_lo .byte $00,$40,$80,$c0,$00,$40,$80,$c0,$00,$40,$80,$c0,$00,$40,$80,$c0,$00,$40,$80,$c0,$00,$40,$80,$c0,$00
row320_hi .byte $a0,$a1,$a2,$a3,$a5,$a6,$a7,$a8,$aa,$ab,$ac,$ad,$af,$b0,$b1,$b2,$b4,$b5,$b6,$b7,$b9,$ba,$bb,$bc,$be

        .here
bm_image_end:
