#!/bin/python3

import subprocess
import sys
import getopt
import json
import threading
import socket
import time
import signal
import os

sub_processes = []
threads = []

def vfio_bind_device(desc, unbind=False):
	from_path="/sys/bus/pci/drivers/{0}".format(desc["orig_driver"])
	to_path="/sys/bus/pci/drivers/vfio-pci"
	if unbind:
		tmp_path = to_path
		to_path = from_path
		from_path = tmp_path

	# unbind
	try:
		path = "{0}/unbind".format(from_path)
		file = open(path, "w")
		file.write(desc["id"])
		file.close()
	except:
		et, ev, et = sys.exc_info();
		print("failed unbinding {0} {1}, {2}".format(desc["orig_driver"], desc["id"], ev))

	# register/remove
	try:
		path = "/sys/bus/pci/drivers/vfio-pci/new_id"
		if unbind:
			path = "/sys/bus/pci/drivers/vfio-pci/remove_id"
		file = open(path, "w")
		file.write(desc["id"])
		file.close()
	except:
		et, ev, et = sys.exc_info();
		print("failed registering {0} {1}, {2}".format(desc["orig_driver"], desc["id"], ev))

	# bind
	try:
		path = "{0}/bind".format(to_path)
		file = open(path, "w")
		file.write(desc["id"])
		file.close()
	except:
		et, ev, et = sys.exc_info();
		print("failed binding {0} {1}, {2}".format(desc["orig_driver"], desc["id"], ev))

def vfio_bind_devices():
	for desc in passthrough_list:
		vfio_bind_device(desc)

def vfio_unbind_devices():
	for desc in passthrough_list:
		vfio_bind_device(desc, True)

def pin_cores(pid):
	pass

def watch_qmp_thread_func(qmp_socket_path, sync_reboot_shutdown):
	qmp_sock = None
	failure = 0

	while failure < 10:
		try:
			qmp_sock = socket.socket(family=socket.AF_UNIX)
			qmp_sock.connect(qmp_socket_path)
			break
		except:
			et, ev, et = sys.exc_info();
			print("failed connecting to qmp socket, {0}".format(ev))
			failure = failure + 1
			qmp_sock = None
			time.sleep(1)

	if qmp_sock is None:
		print("failed connecting to qmp socket, giving up")
		os._exit(1)

	qmp_sock.sendall(b'{ "execute": "qmp_capabilities" }')

	read_buf = b""
	while True:
		recv = qmp_sock.recv(1024 * 4)
		read_buf = read_buf + recv
		read_parsed = None
		try:
			read_parsed = json.loads(read_buf.decode("utf-8"))
		except:
			et, ev, et = sys.exc_info();
			if len(read_buf) != 0:
				print("failed parsing qmp message as json, {0}".format(ev))
				print(read_buf)
				continue

		if read_parsed != None:
			read_buf = b""
			print(read_parsed)

		if len(recv) == 0:
			break

def watch_qmp(qmp_socket_path, sync_reboot_shutdown):
	thread = threading.Thread(target=watch_qmp_thread_func, args=[qmp_socket_path, sync_reboot_shutdown])
	thread.start()
	threads.append(thread)

def gen_storage_arg(args, storage_list):
	args.append("-device")
	args.append("ich9-ahci,id=sata")
	sata_id = 0

	storage_id = 0
	for storage in storage_list:
		storage_id_string = "storage_{0}".format(storage_id)

		args.append("-drive")
		drive = "if=none,format={0},file={1}".format(storage["format"], storage["file"])
		if "cdrom" in storage and storage["cdrom"]:
			drive="{0},media=cdrom".format(drive)
		if "readonly" in storage and storage["readonly"]:
			drive="{0},readonly=on".format(drive)
		if "discard" in storage and storage["discard"]:
			drive="{0},discard=on".format(drive)
		drive="{0},id={1}".format(drive, storage_id_string)
		args.append(drive)

		args.append("-device")
		device = ""
		if storage["interface"] == "ide":
			sata_bus = "sata.{0}".format(sata_id)
			if "cdrom" in storage and storage["cdrom"]:
				device = "ide-cd"
				model = "generic_cd"
			else:
				device = "ide-hd"
				model = "generic_hd"
				rotation_rate = 5400
				if "is_ssd" in storage and storage["is_ssd"]:
					rotation_rate = 1
				device="{0},rotation_rate={1}".format(device,rotation_rate)
			if "model" in storage:
				model = storage["model"]
			device = "{0},model={1},drive={2},bus={3}".format(device, model, storage_id_string, sata_bus)

			sata_id = sata_id + 1

		if storage["interface"] == "virtio-blk":
			device = "virtio-blk,drive={0}".format(storage_id_string)

		args.append(device)

		storage_id = storage_id + 1

def gen_network_arg(args, network_list):
	network_id = 0
	for network in network_list:
		network_id_string = "network_{0}".format(network_id)

		args.append("-netdev")
		netdev=""
		if network["type"] == "user":
			netdev = "user,id={0}".format(network_id_string)
		if network["type"] == "tap":
			netdev = "tap,ifname={0},id={1},script=no,downscript=no".format(network["ifname"], network_id_string)
		args.append(netdev)

		args.append("-device")
		device = "{0},netdev={1}".format(network["guest_device"],network_id_string)
		if "mac" in network:
			device = "{0},mac={1}".format(device, network["mac"])
		args.append(device)

		network_id = network_id + 1

def gen_misc_arg(args):
	args.append("-nodefaults")

	args.append("-machine")
	args.append("q35,accel=kvm")

	args.append("-global")
	args.append("ICH9-LPC.disable_s3=1")
	args.append("-global")
	args.append("ICH9-LPC.disable_s4=1")

	args.append("-global")
	args.append("ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off")

	args.append("-no-reboot")

	args.append("-name")
	args.append("qemu,debug-threads=on")

def gen_uefi_arg(args):
	args.append("-drive")
	args.append("if=pflash,format=raw,file=ovmf.img")

def gen_ui_arg(args):
	args.append("-display")
	args.append("gtk,gl=on")

	args.append("-device")
	args.append("virtio-vga-gl")

def gen_usb_arg(args):
	args.append("-device")
	args.append("qemu-xhci")

def gen_cpu_arg(args, sockets, cores, threads, model, features):
	args.append("-cpu")
	cpu = model
	if features != "":
		cpu = "{0},{1}".format(cpu, features)
	args.append(cpu)

	args.append("-smp")
	args.append("sockets={0},cores={1},threads={2}".format(sockets, cores, threads))

def gen_mem_arg(args, size, path):
	args.append("-m")
	args.append("{0}".format(size))

	if path != "":
		args.append("-mem-path")
		args.append(path)
		args.append("-mem-prealloc")

def gen_qmp_socket_arg(args, socket_path):
	args.append("-chardev")
	args.append("socket,id=qmp_dev,path={0},wait=on,server=on,telnet=off".format(socket_path))

	args.append("-mon")
	args.append("chardev=qmp_dev,mode=control")

def gen_monitor_socket_arg(args, socket_path):
	args.append("-chardev")
	args.append("socket,id=monitor,path={0},wait=off,server=on,telnet=on".format(socket_path))

	args.append("-mon")
	args.append("chardev=monitor")

def gen_serial_socket_args(args, socket_path):
	args.append("-chardev")
	args.append("socket,id=serial,path={0},wait=off,server=on,telnet=on".format(socket_path))

	args.append("-serial")
	args.append("chardev:serial")

def run_qemu_thread_func(args):
	process = subprocess.Popen(args)
	sub_processes.append(process)

def run_qemu(args, qemu_binary):
	full_args = [qemu_binary] + args
	print(full_args)
	thread = threading.Thread(target=run_qemu_thread_func, args=[full_args])
	thread.start()
	threads.append(thread)

def main():
	config = ""

	opts = getopt.getopt(sys.argv[1:], "", [
		"config="
	])

	if len(opts[1]) != 0:
		print("bad arguments: {0}", opts[1])
		os._exit(1)

	for param in opts[0]:
		if param[0] == "--config":
			config = param[1]

	if config == "":
		print("a config file is required")
		os._exit(1)

	config_parsed = None
	try:
		config_file = open(config, "r")
		config_parsed = json.loads(config_file.read())
		config_file.close()
	except:
		et, ev, et = sys.exc_info();
		print("failed parting config, {0}", ev)
		os._exit(1)

	print(config_parsed)

	print(opts)

	args = []
	cpu_config = config_parsed["cpu"]
	gen_cpu_arg(args, cpu_config["sockets"], cpu_config["cores"], cpu_config["threads"], cpu_config["model"], cpu_config["features"])

	mem_config = config_parsed["memory"]
	gen_mem_arg(args, mem_config["size"], mem_config["path"])
	gen_misc_arg(args)
	gen_usb_arg(args)
	gen_uefi_arg(args)
	gen_storage_arg(args, config_parsed["storage_list"])
	gen_network_arg(args, config_parsed["network_list"])
	gen_qmp_socket_arg(args, "qmp_sock")
	gen_monitor_socket_arg(args, "monitor_sock")
	gen_serial_socket_args(args, "serial_sock")
	if "show_ui" in config_parsed and config_parsed["show_ui"]:
		gen_ui_arg(args)
	qemu_binary = "qemu-kvm"
	if "qemu_binary" in config_parsed:
		qemu_binary = config_parsed["qemu_binary"]

	run_qemu(args, qemu_binary)
	watch_qmp("qmp_sock", False)

	for process in sub_processes:
		process.wait()
	for thread in threads:
		thread.join()

def handle_interrupt(signum, stack_frame):
	print("handling signal {0}".format(signum))
	for process in sub_processes:
		process.terminate()
		process.wait()
	os._exit(1)

def setup_signal_handlers():
	signal.signal(signal.SIGINT, handle_interrupt)
	signal.signal(signal.SIGTERM, handle_interrupt)

setup_signal_handlers()
main()
