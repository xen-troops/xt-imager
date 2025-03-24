#!/usr/bin/env python3

import os
import pathlib
import serial
import logging
import argparse
import traceback
from typing import List
from string import printable
import threading
import lzma

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Flash image files through u-boot and tftp',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)


    parser.add_argument(
        'image',
        type=pathlib.Path,
        help='Name of the image file to flash')

    parser.add_argument(
        '-s',
        '--serial',
        default='/dev/ttyUSB0',
        help='Serial console to use')

    parser.add_argument(
        '-b',
        '--baud',
        default=921600,
        help='Baudrate')

    parser.add_argument(
        '-t',
        '--tftp',
        nargs='?',
        type=str,
        default=None,
        const="AUTO",
        help="Use external TFTP server or start our own")

    parser.add_argument(
        '--serverip',
        help='IP of the host that will be used TFTP transfer.')

    parser.add_argument(
        '--ipaddr',
        help='IP of the board that will be used TFTP transfer.')

    parser.add_argument(
        '-v',
        '--verbose',
        action="store_true",
        help='Print the output from the serial console')

    args = parser.parse_args()

    tftp_root = os.getcwd()

    if args.tftp == "AUTO":
        log.info(f"Starting our TFTP server...")

        tftpsrv = PYTFTPServer(tftp_root)
        TFTP_srv_thread = threading.Thread(name="TFTP Server thread", target=tftpsrv.start_tftp_server)
        TFTP_srv_thread.start()

    elif os.path.isdir(args.tftp):
        # use external path
        tftp_root = args.tftp
        log.info(f"Use external TFTP root {tftp_root}")
    else:
        raise Exception("-t parameter is not external TFTP root.")

    do_flash_image(args, tftp_root)

    if args.tftp == "AUTO":
        log.info("Stopping our TFTP server")
        tftpsrv.stop_tftp_server()
        TFTP_srv_thread.join()


class PYTFTPServer(object):
    def __init__(self, folder):
        try:
            import tftpy
            self.tftp_server = tftpy.TftpServer(folder)
        except ImportError:
            print("Can't find tftpy Python module. Please install it.")
            exit(1)

    def start_tftp_server(self):
        # listen to all interfaces and port 69
        self.tftp_server.listen()

    def stop_tftp_server(self):
        self.tftp_server.stop()


def do_flash_image(args, tftp_root):

    log.info(args.image)

    conn = open_connection(args)

    uboot_prompt = "=>"
    # Send 'CR', and check for one of the possible options:
    # - uboot_prompt appears, if u-boot console is already active
    # - u-boot is just starting, so we will get "Hit any key.."
    log.info('Waiting for u-boot prompt...')
    conn_send(conn, "\r")
    conn_wait_for_any(conn, [uboot_prompt, "Hit any key to stop autoboot:"], args.verbose)
    # In case we got "Hit any key", let's stop the boot
    conn_send(conn, "\r")
    conn_wait_for_any(conn, [uboot_prompt], args.verbose)

    image_size = os.path.getsize(args.image)

    ###########
    base_addr = 0x0
    mmc_device = 1
    mmc_part = 0
    mmc_block_size = 512
    ###########

    chunk_filename = "chunk.bin"
    chunk_size_in_bytes = 20*1024*1024

    if str(args.image).endswith(".xz"):
        f_img = lzma.open(args.image)
        image_size = 0
    else:
        f_img = open(args.image, "rb")

    bytes_sent = 0
    block_start = base_addr // mmc_block_size
    out_fullname = os.path.join(tftp_root, chunk_filename)

    if args.serverip:
        conn_send(conn, f"env set serverip {args.serverip}\r")
        conn_wait_for_any(conn, [uboot_prompt], args.verbose)

    if args.ipaddr:
        conn_send(conn, f"env set ipaddr {args.ipaddr}\r")
        conn_wait_for_any(conn, [uboot_prompt], args.verbose)

    # switch to the required MMC device/partition
    conn_send(conn, f"mmc dev {mmc_device} {mmc_part}\r")
    conn_wait_for_any(conn, [uboot_prompt], args.verbose)

    try:
        # do in loop:
        # - read X MB chunk from image file
        # - save chunk to file in tftp root
        # - tell u-boot to 'tftp-and-emmc' chunk
        while True:
            data = f_img.read(chunk_size_in_bytes)

            if not data:
                break

            # create chunk
            f_out = open(out_fullname, "wb")
            data_packed = lzma.compress(data, format=lzma.FORMAT_ALONE, preset = 1)
            f_out.write(data_packed)
            f_out.close()
            conn_send(conn, f"tftp 0x58000000 {chunk_filename}\r")
            # check that all bytes are transmitted
            conn_wait_for_any(conn, [f"Bytes transferred = {len(data_packed)}"], args.verbose)
            conn_wait_for_any(conn, [uboot_prompt], args.verbose)
            # unpack on u-boot side
            conn_send(conn, "lzmadec 0x58000000 0x48000000\r")
            # check that all bytes are uncompressed
            conn_wait_for_any(conn, [f"Uncompressed size: {len(data)}"], args.verbose)
            conn_wait_for_any(conn, [uboot_prompt], args.verbose)

            chunk_size_in_blocks = len(data) // mmc_block_size
            if len(data) % mmc_block_size:
                chunk_size_in_blocks += 1

            conn_send(conn, f"mmc write 0x48000000 0x{block_start:X} 0x{chunk_size_in_blocks:X}\r")
            # check that all blocks are written properly
            conn_wait_for_any(conn, [f"{chunk_size_in_blocks} blocks written: OK"], args.verbose)
            conn_wait_for_any(conn, [uboot_prompt], args.verbose)

            bytes_sent += len(data)
            block_start += chunk_size_in_blocks

            if args.verbose:
                # in the verbose mode we need to print progress on the new line
                # and move to the next line
                print('')
                end_of_string = '\n'
            else:
                # in the regular mode we print progress in one line
                end_of_string = '\r'

            if image_size:
                print(f"Progress: {bytes_sent:_}/{image_size:_} ({bytes_sent * 100 // image_size}%)", end=end_of_string)
            else:
                print(f"Progress: {bytes_sent:_}", end=end_of_string)
    finally:
        # remove chunk from tftp root
        os.remove(out_fullname)

    f_img.close()
    conn.close()

    if not args.verbose:
        # move to the next line, below the progress
        print('')
    log.info("Image was flashed successfully.")


def open_connection(args):
    dev_name = args.serial
    baud = args.baud

    log.info(f"Using serial port {dev_name} with baudrate {baud}")
    return serial.Serial(port=dev_name, baudrate=baud, timeout=20)


def conn_wait_for_any(conn, expect: List[str], verbose: bool):
    rcv_str = ""
    # stay in the read loop until any of expected string is received
    # in other words - all expected substrings are not in received buffer
    while all([x not in rcv_str for x in expect]):
        data = conn.read(1)
        if not data:
            raise TimeoutError(f"Timeout waiting for `{expect}` from the device")
        rcv_char = chr(data[0])
        if verbose and (rcv_char in printable or rcv_char == '\b'):
            print(rcv_char, end='', flush=True)
        rcv_str += rcv_char


def conn_send(conn, data):
    conn.write(data.encode("ascii"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.fatal(e)
        log.fatal(traceback.format_exc())
