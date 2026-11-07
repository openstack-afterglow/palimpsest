#include <stdio.h>

#if !defined(__linux__) || !defined(__aarch64__)
#error "This fixture requires a Linux arm64 compiler target."
#endif

int main(int argc, char **argv)
{
    FILE *input;
    FILE *report;
    int value;
    int scanned;
    unsigned int count = 0;
    long long sum = 0;

    if (argc != 3) {
        fputs("usage: probe INPUT REPORT\n", stderr);
        return 2;
    }
    input = fopen(argv[1], "r");
    if (input == NULL) {
        perror("input");
        return 1;
    }
    while ((scanned = fscanf(input, "%d", &value)) == 1) {
        if (count == 16) {
            fputs("input exceeds 16 integers\n", stderr);
            fclose(input);
            return 2;
        }
        sum += value;
        count++;
    }
    if (scanned != EOF || ferror(input) || count == 0) {
        fputs("input must contain 1 to 16 integers\n", stderr);
        fclose(input);
        return 2;
    }
    if (fclose(input) != 0) {
        perror("input close");
        return 1;
    }
    report = fopen(argv[2], "w");
    if (report == NULL) {
        perror("report");
        return 1;
    }
    if (fprintf(report, "count=%u\nsum=%lld\n", count, sum) < 0) {
        fclose(report);
        return 1;
    }
    if (fclose(report) != 0) {
        perror("report close");
        return 1;
    }
    if (printf("palimpsest-vz-native-root\ncount=%u\nsum=%lld\n", count, sum) < 0
        || fflush(stdout) != 0) {
        return 1;
    }
    return 0;
}
