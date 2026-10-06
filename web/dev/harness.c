/* Calls the web entry point twice in a row, as the page does, and waits for each run's
 * "done" the way the worker would. */
#include <pthread.h>
#include <stdio.h>
int pngfit_start(int argc, char **argv);
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static int done = 0, code = -1;
void web_done_stub(int rc)
{
    pthread_mutex_lock(&mu);
    done = 1;
    code = rc;
    pthread_cond_signal(&cv);
    pthread_mutex_unlock(&mu);
}
int main(int argc, char **argv)
{
    if (argc < 4)
        return 2;
    for (int run = 0; run < 2; run++) {
        char *args[] = {"pngfit", "-s", run ? argv[3] : "90%", "--json", "--progress-lines", argv[1], argv[2], NULL};
        done = 0;
        if (pngfit_start(7, args))
            return 3;
        pthread_mutex_lock(&mu);
        while (!done)
            pthread_cond_wait(&cv, &mu);
        pthread_mutex_unlock(&mu);
        printf("run %d finished with %d\n", run, code);
        fflush(stdout);
    }
    return 0;
}
