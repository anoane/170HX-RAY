obj-m := 170hx_xray.o 170hx_xray_therm.o
KDIR ?= /lib/modules/$(shell uname -r)/build
HOSTCC ?= cc
UAPI_TEST := .170hx_xray_uapi_abi_test

.PHONY: all abi-test clean

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

abi-test:
	$(HOSTCC) $(HOSTCFLAGS) -std=c11 -Wall -Wextra -Werror -I$(CURDIR) \
		170hx_xray_uapi_abi_test.c -o $(UAPI_TEST)
	./$(UAPI_TEST)
	$(RM) $(UAPI_TEST)

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	$(RM) $(UAPI_TEST)
