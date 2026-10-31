#ifndef MINIHTTP_H
#define MINIHTTP_H

#include <stddef.h>

#define MH_MAX_URL 2048

typedef enum {
  MH_OK = 0,
  MH_URL_MALFORMAT = 3,
  MH_OUT_OF_MEMORY = 27,
  MH_BAD_FUNCTION_ARGUMENT = 43
} mh_code;

typedef enum {
  MH_OPT_URL = 1,
  MH_OPT_TIMEOUT = 2
} mh_option;

typedef struct mh_easy mh_easy;

mh_easy *mh_easy_init(void);
mh_code mh_easy_setopt(mh_easy *h, mh_option opt, const char *value);
mh_code mh_easy_perform(mh_easy *h);
void mh_easy_cleanup(mh_easy *h);

mh_code mh_url_parse(const char *url, char **host, char **path);

#endif
