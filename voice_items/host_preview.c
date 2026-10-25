/* Host-only harness around the exact radio decoder.
 *   host_preview ID...        raw 8 kHz u8 duty stream for those word ids
 *   host_preview --pick ADDR  word ids the radio would queue for a one-item list -> ADDR
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
int vl_sample(void);
void vl_start(uint32_t id);
const uint16_t *vl_pick_for(uintptr_t list, uint32_t cnt, uint32_t cur, uint32_t idx);
int main(int argc, char **argv)
{
    if (argc == 3 && strcmp(argv[1], "--pick") == 0) {
        uint32_t list[1] = { (uint32_t)strtoul(argv[2], 0, 0) };
        const uint16_t *pl = vl_pick_for((uintptr_t)list, 1, 0, 0);
        for (int i = 2; pl[i] != 0xFFFF; i += 4) printf("%u ", pl[i] - 0xF000u);
        printf("\n");
        return 0;
    }
    for (int a = 1; a < argc; a++) {
        vl_start((uint32_t)atoi(argv[a]));
        int d;
        while ((d = vl_sample()) >= 0) putchar(d);
        for (int i = 0; i < 400; i++) putchar(0);
    }
    return 0;
}
