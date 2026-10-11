// Board bring-up of the RMCS Slave Lite BSP.
// Sets up the SDK board, then the pins and the clocks of the peripherals the board uses.
// The pins come from init_bsp_pins() and the clocks from init_clocks(); both are generated
// by the HPM Pinmux Tool from boards/rmcs_slave_lite/tool_config.hpmpc. The LibXR objects
// for these peripherals are created in app_main().
#include "app_main.h"
#include "board.h"
#include "clock.h"
#include "pinmux.h"

int main(void)
{
  // Pins: CAN0 PA00/PA01, CAN2 PA08/PA09, CAN3 PB15/PB14, UART0 PY00/PY01 (console),
  // UART2 PB08/PB09, DBUS UART7 RX PA30, SPI1 PA27-PA29, IMU CS PB11/PB13 (high) and INT
  // PB12/PB10, USB switch PA31, buzzer GPTMR0 COMP2 PA10, IMU heater GPTMR2 COMP2 PA26,
  // USB0 PA24/PA25. They come first so the console pins exist when board_init() prints.
  init_bsp_pins();

  // Clocks (init_clocks(): PLLs, CPU/bus and peripheral clocks), console on UART0, PMP
  board_init();

#if !RMCS_KEEP_JTAG
  // PA04-PA07 stop being JTAG: CAN1 PA04/PA05, WS2812 PWM1 P6 PA06 and KEY PA07
  init_bsp_jtag_shared_pins();
#endif

  app_main();
  return 0;
}
