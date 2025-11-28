# tap device wait
while [ -z "$(ip link list VM_TAP)" ]
do
	sleep 1
done

# adjust barsize
echo '0000:0a:00.0' > "/sys/bus/pci/drivers/amdgpu/unbind"
echo 13 > "/sys/devices/pci0000:00/0000:00:03.1/0000:0a:00.0/resource0_resize"
