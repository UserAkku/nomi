#include <stdlib.h>
#include <string.h>

#include "minihttp.h"

struct mh_easy {
  char *url;
  long timeout;
};

mh_easy *mh_easy_init(void)
{
  return calloc(1, sizeof(mh_easy));
}

/* Store a copy of the option value. The URL is heap-allocated with its exact
   length, so there is no fixed-size buffer to overflow. */
mh_code mh_easy_setopt(mh_easy *h, mh_option opt, const char *value)
{
  if(!h || !value)
    return MH_BAD_FUNCTION_ARGUMENT;
  switch(opt) {
  case MH_OPT_URL: {
    size_t len = strlen(value);
    char *copy;
    if(len > MH_MAX_URL)
      return MH_URL_MALFORMAT;
    copy = malloc(len + 1);
    if(!copy)
      return MH_OUT_OF_MEMORY;
    memcpy(copy, value, len + 1);
    free(h->url);
    h->url = copy;
    return MH_OK;
  }
  case MH_OPT_TIMEOUT:
    h->timeout = strtol(value, NULL, 10);
    return MH_OK;
  }
  return MH_BAD_FUNCTION_ARGUMENT;
}

mh_code mh_easy_perform(mh_easy *h)
{
  char *host = NULL;
  char *path = NULL;
  mh_code rc;
  if(!h || !h->url)
    return MH_BAD_FUNCTION_ARGUMENT;
  rc = mh_url_parse(h->url, &host, &path);
  free(host);
  free(path);
  return rc;
}

void mh_easy_cleanup(mh_easy *h)
{
  if(!h)
    return;
  free(h->url);
  free(h);
}
