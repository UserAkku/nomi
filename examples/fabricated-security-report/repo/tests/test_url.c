#include <assert.h>
#include <stdlib.h>
#include <string.h>

#include "minihttp.h"

int main(void)
{
  char *host = NULL;
  char *path = NULL;
  char long_url[MH_MAX_URL + 16];
  assert(mh_url_parse("https://example.com/a", &host, &path) == MH_OK);
  assert(strcmp(host, "example.com") == 0);
  free(host);
  free(path);
  memset(long_url, 'a', sizeof(long_url) - 1);
  long_url[sizeof(long_url) - 1] = '\0';
  assert(mh_url_parse(long_url, &host, &path) == MH_URL_MALFORMAT);
  return 0;
}
