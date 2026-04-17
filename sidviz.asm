; sidviz.asm
; 64tass assembler
; autostart SYS 2064
;
; version 1.0.0 (2026-04-16-1)
;
; Memory map:
;   $C000 = frame ready flag  (Python writes 1, ASM clears to 0)
;   $C001 = color toggle flag (Python writes 1=white, 2=rainbow, ASM applies+clears)
;   $C100 = frame buffer, 1000 bytes of PETSCII screen codes
;
; Python workflow per frame:
;   1. writemem $C100, 1000 bytes of screen data
;   2. writemem $C000, $01
;   ASM copies buffer -> screen RAM, clears flag

src_lo  = $fb
src_hi  = $fc
dst_lo  = $fd
dst_hi  = $fe

screen      = $0400
color       = $d800
border      = $d020
bgcol       = $d021
frame_flag  = $c000
color_flag  = $c001
frame_buf   = $c100

* = $0801
        .word line10, 10
        .byte $9e
        .text "2064"
        .byte 0
line10  .word 0

* = $0810

init:
        sei
        lda #$00
        sta border
        sta bgcol
        sta frame_flag
        sta color_flag
        jsr fill_color_rainbow
        jsr clear_screen
        cli

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

copy_frame:
        lda #<frame_buf
        sta src_lo
        lda #>frame_buf
        sta src_hi
        lda #<screen
        sta dst_lo
        lda #>screen
        sta dst_hi
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
        ldy #0
cf_rm:  lda (src_lo),y
        sta (dst_lo),y
        iny
        cpy #232
        bne cf_rm
        lda #0
        sta frame_flag
        rts

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

fill_color_rainbow:
        lda #<color
        sta dst_lo
        lda #>color
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
fcr_p3: lda rainbow_table,x
        sta (dst_lo),y
        inx
        cpx #40
        bcc fcr_p3n
        ldx #0
fcr_p3n:iny
        cpy #232
        bne fcr_p3
        rts

fill_color_white:
        lda #<color
        sta dst_lo
        lda #>color
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
        cpy #232
        bne fcw_rm
        rts

rainbow_table:
        .byte  2,  2,  8,  8,  7,  7,  7,  7
        .byte  5,  5,  5,  5, 13, 13, 14, 14
        .byte  6,  6,  6,  6,  4,  4,  4,  4
        .byte 10, 10,  2,  2,  8,  8,  7,  7
        .byte  5,  5, 13, 13, 14, 14,  6,  6
