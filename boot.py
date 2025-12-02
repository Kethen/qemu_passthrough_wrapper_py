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
import re
import random

random.seed()

sub_processes = []
threads = []
qemu_process = None
swtpm_process = None
tpm_socket_path = "tpm_sock"

def read_if_in_dict(d, name, default):
	if name in d:
		return d[name]
	else:
		return default

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
		file.write(desc["host"])
		file.close()
	except:
		et, ev, et = sys.exc_info();
		print("failed unbinding {0} {1}, {2}".format(desc["orig_driver"], desc["host"], ev))

	# register/remove
	try:
		path = "/sys/bus/pci/drivers/vfio-pci/new_id"
		if unbind:
			path = "/sys/bus/pci/drivers/vfio-pci/remove_id"
		file = open(path, "w")
		id_space = desc["id"].replace(":", " ")
		file.write(id_space)
		file.close()
	except:
		et, ev, et = sys.exc_info();
		action = "registering"
		if unbind:
			action = "unregistering"
		print("failed {0} {1} {2} {3}, {4}".format(action, desc["orig_driver"], desc["id"], desc["host"], ev))

	# bind
	try:
		path = "{0}/bind".format(to_path)
		file = open(path, "w")
		file.write(desc["host"])
		file.close()
	except:
		et, ev, et = sys.exc_info();
		print("failed binding {0} {1}, {2}".format(desc["orig_driver"], desc["host"], ev))

def vfio_bind_devices(passthrough_list):
	for port in passthrough_list:
		for device in port:
			vfio_bind_device(device)

def vfio_unbind_devices(passthrough_list):
	for port in passthrough_list:
		for device in port:
			vfio_bind_device(device, True)

def pipe_consumer_thread_func(pipe, out):
	buf = b''
	while True:
		r = pipe.read()
		if len(r) == 0:
			break
		buf = buf + r
	out.append(buf)

def pin_cores_thread_func(pinning, num_cores):
	failure = 0
	sample = "  64182   64195 pts/2    00:00:00 CPU 0/KVM"
	regex = r"\s+\d+\s+(\d+)\s+\S+\s+\S+\s+(.+)"
	matcher = re.compile(regex)
	cpu_regex = r"CPU\s+(\d+)/KVM"
	cpu_matcher = re.compile(cpu_regex)

	while failure < 40:
		if qemu_process is None:
			failure = failure + 1
			time.sleep(0.25)
			continue

		if qemu_process.returncode is not None:
			break

		process = subprocess.Popen(args=["ps", "-L", "-w", "-p", "{0}".format(qemu_process.pid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		stdout = []
		stderr = []
		stdout_reader_thread = threading.Thread(target = pipe_consumer_thread_func, args = [process.stdout, stdout])
		stderr_reader_thread = threading.Thread(target = pipe_consumer_thread_func, args = [process.stderr, stderr])
		stdout_reader_thread.start()
		stderr_reader_thread.start()
		stdout_reader_thread.join()
		stderr_reader_thread.join()
		stdout = stdout[0].decode("utf-8")
		stderr = stderr[0].decode("utf-8")

		cpu_threads = []
		other_threads = []
		for line in stdout.split("\n"):
			result = matcher.match(line)
			if result is None:
				continue
			thread_name = result.group(2)
			cpu_result = cpu_matcher.match(thread_name)
			if cpu_result is None:
				other_threads.append({
					"lwp":result.group(1)
				})
				continue
			cpu_threads.append({
				"lwp":result.group(1),
				"cpu":cpu_result.group(1)
			})

		if len(cpu_threads) != num_cores:
			failure = failure + 1
			time.sleep(0.25)
			continue


		if "others" in pinning:
			print("python process {0} -> hCPU {1}".format(os.getpid(), pinning["others"]))
			subprocess.run(["taskset", "-acp", pinning["others"], "{0}".format(os.getpid())])
			for thread in other_threads:
				print("other {0} -> hCPU {1}".format(thread["lwp"], pinning["others"]))
				subprocess.run(["taskset", "-pc", pinning["others"], "{0}".format(thread["lwp"])])

		for thread in cpu_threads:
			cpu = thread["cpu"]
			lwp = thread["lwp"]
			if cpu in pinning:
				print("vCPU {0} -> hCPU {1}".format(cpu, pinning[cpu]))
				subprocess.run(["taskset", "-pc", "{0}".format(pinning[cpu]), lwp])
		print("cpus pinned")

		time.sleep(10)

	if failure == 40:
		print("failed pinning cpu cores")

def pin_cores(pinning, num_cores):
	thread = threading.Thread(target=pin_cores_thread_func, args=[pinning, num_cores])
	thread.start()
	threads.append(thread)

def watch_qmp_thread_func(qmp_socket_path, guest_reboot_script, guest_shutdown_script):
	qmp_sock = None
	failure = 0

	while failure < 40:
		try:
			qmp_sock = socket.socket(family=socket.AF_UNIX)
			qmp_sock.connect(qmp_socket_path)
			break
		except:
			et, ev, et = sys.exc_info();
			print("failed connecting to qmp socket, {0}".format(ev))
			failure = failure + 1
			qmp_sock = None
			time.sleep(0.25)

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
			if read_if_in_dict(read_parsed, "event", "") == "SHUTDOWN":
				if read_parsed["data"]["reason"] == "guest-shutdown":
					subprocess.run(guest_shutdown_script, shell=True)
				if read_parsed["data"]["reason"] == "guest-reset":
					subprocess.run(guest_reboot_script, shell=True)
		if len(recv) == 0:
			break

def watch_qmp(qmp_socket_path, guest_reboot_script, guest_shutdown_script):
	thread = threading.Thread(target=watch_qmp_thread_func, args=[qmp_socket_path, guest_reboot_script, guest_shutdown_script])
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
		if read_if_in_dict(storage, "cdrom", False):
			drive="{0},media=cdrom".format(drive)
		if read_if_in_dict(storage, "readonly", False):
			drive="{0},readonly=on".format(drive)
		if read_if_in_dict(storage, "discard", False):
			drive="{0},discard=on".format(drive)
		drive="{0},id={1}".format(drive, storage_id_string)
		args.append(drive)

		args.append("-device")
		device = ""
		interface = read_if_in_dict(storage, "interface", "ide")
		if interface == "ide":
			sata_bus = "sata.{0}".format(sata_id)
			if read_if_in_dict(storage, "cdrom", False):
				device = "ide-cd"
				model = "generic_cd"
				serial = "generic_cd"
			else:
				device = "ide-hd"
				model = "generic_hd"
				serial = "generic_hd"
				rotation_rate = 5400
				if read_if_in_dict(storage, "is_ssd", False):
					rotation_rate = 1
				device="{0},rotation_rate={1}".format(device,rotation_rate)
			model = read_if_in_dict(storage, "model", model)
			serial = read_if_in_dict(storage, "serial", serial)
			device = "{0},model={1},drive={2},bus={3},serial={4}".format(device, model, storage_id_string, sata_bus, serial)

			sata_id = sata_id + 1

		if interface == "virtio":
			device = "virtio-blk,drive={0}".format(storage_id_string)

		if interface == "nvme":
			serial = read_if_in_dict(storage, "serial", "generic_nvme")
			device = "nvme,drive={0},serial={1}".format(storage_id_string, serial)

		if interface == "usb":
			serial = read_if_in_dict(storage, "serial", "generic_usb")
			device = "usb-storage,drive={0},serial={1},removable=true".format(storage_id_string, serial)

		args.append(device)

		storage_id = storage_id + 1

def gen_mac():
	mac = "a2"
	for i in range(5):
		mac = "{0}:{1:02X}".format(mac, random.randbytes(1)[0])
	return mac

def gen_network_arg(args, network_list):
	network_id = 0
	for network in network_list:
		network_id_string = "network_{0}".format(network_id)

		args.append("-netdev")
		netdev=""
		type = read_if_in_dict(network, "type", "user")
		if type == "user":
			netdev = "user,id={0}".format(network_id_string)
		if type == "tap":
			netdev = "tap,ifname={0},id={1},script=no,downscript=no".format(network["ifname"], network_id_string)
		args.append(netdev)

		args.append("-device")
		guest_device = read_if_in_dict(network, "guest_device", "usb-net")
		device = "{0},netdev={1}".format(guest_device,network_id_string)
		device = "{0},mac={1}".format(device, read_if_in_dict(network, "mac", gen_mac()))
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

def gen_uefi_arg(args, readonly):
	args.append("-drive")
	args.append("if=pflash,format=raw,file=ovmf.img")

def gen_ui_arg(args):
	args.append("-display")
	args.append("gtk,gl=on")

	args.append("-device")
	args.append("virtio-vga-gl")

	args.append("-audiodev")
	args.append("pa,id=pa")
	args.append("-device")
	args.append("ich9-intel-hda")
	args.append("-device")
	args.append("hda-duplex,audiodev=pa")

	args.append("-device")
	args.append("virtio-tablet-pci")

def gen_no_ui_arg(args):
	args.append("-display")
	args.append("none")

def gen_usb_arg(args):
	args.append("-device")
	args.append("nec-usb-xhci")

def gen_cpu_arg(args, cpu_dict):
	args.append("-cpu")
	model = read_if_in_dict(cpu_dict, "model", "host")
	features = read_if_in_dict(cpu_dict, "features", "")
	sockets = read_if_in_dict(cpu_dict, "sockets", 1)
	cores = read_if_in_dict(cpu_dict, "cores", 1)
	threads = read_if_in_dict(cpu_dict, "threads", 1)

	cpu = model
	if features != "":
		cpu = "{0},{1}".format(cpu, features)
	args.append(cpu)

	args.append("-smp")
	args.append("sockets={0},cores={1},threads={2}".format(sockets, cores, threads))

def gen_mem_arg(args, mem_dict):
	size = read_if_in_dict(mem_dict, "size", "128M")
	path = read_if_in_dict(mem_dict, "path", "")

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

def run_qemu(args, qemu_binary):
	global qemu_process
	full_args = [qemu_binary] + args
	for arg in full_args:
		print(arg)
	process = subprocess.Popen(full_args)
	qemu_process = process

def run_swtpm(dir, socket_path, swtpm_binary):
	global swtpm_process
	try:
		os.remove(socket_path)
	except:
		pass
	try:
		os.mkdir(dir)
	except:
		et, ev, et = sys.exc_info();
		print("failed creating swtpm directory, {0}".format(ev))
	process = subprocess.Popen([swtpm_binary, "socket", "--tpmstate", "dir={0}".format(dir), "--ctrl", "type=unixio,path={0}".format(socket_path), "--tpm2"])
	swtpm_process = process

	failure = 0
	while failure < 40:
		if os.path.exists(socket_path):
			break
		failure = failure + 1
		time.sleep(0.25)

	if failure == 40:
		print("swtpm start seems to have failed")

def gen_tpm_arg(args, swtpm_socket_path):
	args.append("-chardev")
	args.append("socket,id=tpm_char_dev,path={0}".format(swtpm_socket_path))
	args.append("-tpmdev")
	args.append("emulator,id=tpm_dev,chardev=tpm_char_dev")
	args.append("-device")
	args.append("tpm-tis,tpmdev=tpm_dev")

def gen_passthrough_arg(args, passthrough_list):
	port_id = 0
	args.append("-device")
	args.append("pcie-root-port,hotplug=off,id=pcie_root_port")

	for port in passthrough_list:
		function_num = 0
		for device in port:
			args.append("-device")
			arg = "vfio-pci-nohotplug,host={0},multifunction=on".format(device["host"])
			if "romfile" in device:
				arg = "{0},romfile={1},rombar=1".format(arg, device["romfile"])
			if read_if_in_dict(device, "pcie", False):
				arg = "{0},bus=pcie_root_port,addr={1}.{2}".format(arg, hex(port_id)[2:], hex(function_num)[2:])
			function_num = function_num + 1
			args.append(arg)
		port_id = port_id + 1

def gen_smbios_arg(args, smbios_dict):
	type_0 = read_if_in_dict(smbios_dict, "type_0", {})
	type_1 = read_if_in_dict(smbios_dict, "type_1", {})
	type_2 = read_if_in_dict(smbios_dict, "type_2", {})
	type_3 = read_if_in_dict(smbios_dict, "type_3", {})
	type_4 = read_if_in_dict(smbios_dict, "type_4", {})
	type_17 = read_if_in_dict(smbios_dict, "type_17", {})

	# t0
	vendor = read_if_in_dict(type_0, "vendor", "default_t0_vendor")
	version = read_if_in_dict(type_0, "version", "default_t0_version")
	date = read_if_in_dict(type_0, "date", "default_t0_date")
	release = read_if_in_dict(type_0, "release", "1.0")
	uefi = read_if_in_dict(type_0, "uefi", "on")
	vm = read_if_in_dict(type_0, "vm", "on")
	args.append("-smbios")
	args.append("type=0,vendor={0},version={1},date={2},release={3},uefi={4},vm={5}".format(vendor, version, date, release, uefi, vm))

	# t1
	manufacturer = read_if_in_dict(type_1, "manufacturer", "default_t1_manufacturer")
	product = read_if_in_dict(type_1, "product", "default_t1_product")
	version = read_if_in_dict(type_1, "version", "default_t1_version")
	serial = read_if_in_dict(type_1, "serial", "default_t1_serial")
	uuid = read_if_in_dict(type_1, "uuid", "11111111-1111-1111-1111-111111111111")
	sku = read_if_in_dict(type_1, "sku", "default_t1_sku")
	family = read_if_in_dict(type_1, "family", "default_t1_family")
	args.append("-smbios")
	args.append("type=1,manufacturer={0},product={1},version={2},serial={3},uuid={4},sku={5},family={6}".format(manufacturer, product, version, serial, uuid, sku, family))

	# t2
	manufacturer = read_if_in_dict(type_2, "manufacturer", "default_t2_manufacturer")
	product = read_if_in_dict(type_2, "product", "default_t2_product")
	version = read_if_in_dict(type_2, "version", "default_t2_version")
	serial = read_if_in_dict(type_2, "serial", "default_t2_serial")
	asset = read_if_in_dict(type_2, "asset", "default_t2_asset")
	location = read_if_in_dict(type_2, "location", "default_t2_location")
	args.append("-smbios")
	args.append("type=2,manufacturer={0},product={1},version={2},serial={3},asset={4},location={5}".format(manufacturer, product, version, serial, asset, location))

	# t3
	manufacturer = read_if_in_dict(type_3, "manufacturer", "default_t3_manufacturer")
	version = read_if_in_dict(type_3, "version", "default_t3_version")
	serial = read_if_in_dict(type_3, "serial", "default_t3_serial")
	asset = read_if_in_dict(type_3, "asset", "default_t3_asset")
	sku = read_if_in_dict(type_3, "sku", "default_t3_sku")
	args.append("-smbios")
	args.append("type=3,manufacturer={0},version={1},serial={2},asset={3},sku={4}".format(manufacturer, version, serial, asset, sku))

	# t4
	manufacturer = read_if_in_dict(type_4, "manufacturer", "default_t4_manufacturer")
	version = read_if_in_dict(type_4, "version", "default_t4_version")
	args.append("-smbios")
	args.append("type=4,manufacturer={0},version={1}".format(manufacturer, version))

	# t17
	manufacturer = read_if_in_dict(type_17, "manufacturer", "default_t17_manufacturer")
	args.append("-smbios")
	args.append("type=17,manufacturer={0}".format(manufacturer))

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
		print("failed parsing config, {0}", ev)
		os._exit(1)

	print(config_parsed)

	print(opts)

	args = []
	cpu_config = read_if_in_dict(config_parsed, "cpu", {})
	gen_cpu_arg(args, cpu_config)

	mem_config = read_if_in_dict(config_parsed, "memory", {})
	gen_mem_arg(args, mem_config)

	gen_misc_arg(args)
	gen_usb_arg(args)
	gen_uefi_arg(args, read_if_in_dict(config_parsed, "readonly_nvram", True))
	gen_storage_arg(args, read_if_in_dict(config_parsed, "storage_list", []))
	gen_network_arg(args, read_if_in_dict(config_parsed, "network_list", []))
	gen_qmp_socket_arg(args, "qmp_sock")
	gen_monitor_socket_arg(args, "monitor_sock")
	gen_serial_socket_args(args, "serial_sock")
	gen_smbios_arg(args, read_if_in_dict(config_parsed, "smbios", {}))

	if read_if_in_dict(config_parsed, "show_ui", True):
		gen_ui_arg(args)
	else:
		gen_no_ui_arg(args)

	if "pre_script" in config_parsed:
		subprocess.run(config_parsed["pre_script"], shell=True)

	passthrough_list = read_if_in_dict(config_parsed, "passthrough_list", [])
	vfio_bind_devices(passthrough_list)
	gen_passthrough_arg(args, passthrough_list)

	if read_if_in_dict(config_parsed, "tpm", False):
		swtpm_binary = read_if_in_dict(config_parsed, "swtpm_binary", "swtpm")
		run_swtpm("tpm_state", tpm_socket_path, swtpm_binary)
		gen_tpm_arg(args, tpm_socket_path)

	run_qemu(args, read_if_in_dict(config_parsed, "qemu_binary", "qemu-kvm"))

	guest_reboot_script = read_if_in_dict(config_parsed, "guest_reboot_script", "")
	guest_shutdown_script = read_if_in_dict(config_parsed, "guest_shutdown_script", "")
	watch_qmp("qmp_sock", guest_reboot_script, guest_shutdown_script)

	if "pinning" in cpu_config:
		pin_cores(cpu_config["pinning"], int(cpu_config["sockets"]) * int(cpu_config["cores"]) * int(cpu_config["threads"]))

	for process in sub_processes:
		process.wait()

	if qemu_process is not None:
		qemu_process.wait()

	if swtpm_process is not None:
		# should be stopped by qemu going down, if not it might as well be stuck
		swtpm_process.kill()

	vfio_unbind_devices(passthrough_list)

	try:
		os.remove(tpm_socket_path)
	except:
		pass
	os._exit(1)

def handle_interrupt(signum, stack_frame):
	print("handling signal {0}".format(signum))
	for process in sub_processes:
		process.terminate()
		process.wait()
	if qemu_process is not None:
		qemu_process.terminate()
		# there should be a wait happening on the main thread, so if we don't check if the subprocess is waited already, we might actually get stuck
		if qemu_process.returncode is not None:
			qemu_process.wait()
	if swtpm_process is not None:
		# should be stopped by qemu going down, if not it might as well be stuck
		swtpm_process.kill()
	try:
		os.remove(tpm_socket_path)
	except:
		pass
	os._exit(1)

def setup_signal_handlers():
	signal.signal(signal.SIGINT, handle_interrupt)
	signal.signal(signal.SIGTERM, handle_interrupt)

setup_signal_handlers()
main()
