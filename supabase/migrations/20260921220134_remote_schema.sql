SET local check_function_bodies = off;

CREATE TABLE "public"."profiles" (
  "id"         uuid                     NOT NULL,
  "username"   text                     NOT NULL,
  "created_at" timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "profiles_pkey" PRIMARY KEY (id),
  CONSTRAINT "profiles_username_check" CHECK ((length(username) >= 4)),
  CONSTRAINT "profiles_username_key" UNIQUE (username)
);

ALTER TABLE "public"."profiles"
  ENABLE ROW LEVEL SECURITY;

CREATE TYPE "public"."user_role" AS ENUM (
  'student',
  'teacher',
  'researcher',
  'professional',
  'other'
);

ALTER TABLE "public"."profiles"
  ADD COLUMN "role" public.user_role NOT NULL DEFAULT 'student'::public.user_role;

CREATE OR REPLACE FUNCTION public.get_email_from_username (
  p_username text
)
  RETURNS text
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
  select au.email
  from auth.users au
  join public.profiles p
    on p.id = au.id
  where p.username = lower(trim(p_username))
  limit 1;
$function$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
declare
    v_role public.user_role;
begin
    v_role := (new.raw_user_meta_data ->> 'role')::public.user_role;
    insert into public.profiles (id, username, role)
    values (
            new.id,
            lower(trim(new.raw_user_meta_data ->> 'username')),
            v_role
    );
    update auth.users
    set raw_app_meta_data = raw_app_meta_data || jsonb_build_object('role', v_role::text)
    where id = new.id;
    
    return new;
end;
$function$;

CREATE OR REPLACE FUNCTION public.username_exists (
  p_username text
)
  RETURNS boolean
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
    select exists (
        select 1
        from public.profiles
        where username = lower(trim(p_username))
    );
$function$;

ALTER TABLE "public"."profiles"
  ADD CONSTRAINT "profiles_id_fkey" FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();

CREATE POLICY "Users can update their own profile" ON "public"."profiles"
  FOR UPDATE
  TO PUBLIC
  USING ((auth.uid() = id))
  WITH CHECK ((auth.uid() = id));

CREATE POLICY "Users can view their own profile" ON "public"."profiles"
  FOR SELECT
  TO PUBLIC
  USING ((auth.uid() = id));

COMMENT ON COLUMN "public"."profiles"."role" IS 'Student, Researcher, Teacher, Professional, Others';

GRANT EXECUTE ON FUNCTION "public"."get_email_from_username"(text) TO PUBLIC, "postgres", "service_role";

GRANT EXECUTE ON FUNCTION "public"."handle_new_user"() TO PUBLIC, "anon", "authenticated", "postgres", "service_role";

GRANT EXECUTE ON FUNCTION "public"."username_exists"(text) TO PUBLIC, "anon", "authenticated", "postgres", "service_role";

REVOKE ALL ON TABLE "public"."profiles" FROM "authenticated";

GRANT SELECT, UPDATE ON TABLE "public"."profiles" TO "authenticated";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."profiles" TO "postgres", "service_role";

GRANT USAGE ON TYPE "public"."user_role" TO "postgres";

