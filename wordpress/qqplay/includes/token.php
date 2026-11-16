<?php
/**
 * The token the site signs for whoever is signed in: a JSON Web Token with
 * HMAC-SHA256 and the integration's secret, valid for ten minutes.
 *
 * Filters, for what a site does differently:
 *
 *     qqplay_user_is_teacher  ( bool $teacher, WP_User $user )
 *     qqplay_user_classes     ( string[] $classes, WP_User $user )
 *     qqplay_token_claims     ( array $claims, WP_User $user )
 *
 * @package QQplay
 */

defined( 'ABSPATH' ) || exit;

/**
 * Base64 as tokens write it.
 *
 * @param string $data Bytes.
 * @return string
 */
function qqplay_b64( $data ) {
	// phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- the JWT format.
	return rtrim( strtr( base64_encode( $data ), '+/', '-_' ), '=' );
}

/**
 * Sign claims with the integration's secret.
 *
 * @param array $claims What the token says.
 * @return string
 */
function qqplay_sign( array $claims ) {
	$header = array(
		'alg' => 'HS256',
		'typ' => 'JWT',
		'kid' => qqplay_setting( 'key' ),
	);
	$signed = qqplay_b64( wp_json_encode( $header ) ) . '.' . qqplay_b64( wp_json_encode( $claims ) );
	return $signed . '.' . qqplay_b64( hash_hmac( 'sha256', $signed, qqplay_setting( 'secret' ), true ) );
}

/**
 * Whether someone moderates the chat and the comments.
 *
 * @param WP_User $user Who.
 * @return bool
 */
function qqplay_user_is_teacher( WP_User $user ) {
	$teacher = (bool) array_intersect( (array) $user->roles, qqplay_teacher_roles() );
	if ( ! $teacher && is_multisite() && is_super_admin( $user->ID ) ) {
		$teacher = true;
	}
	return (bool) apply_filters( 'qqplay_user_is_teacher', $teacher, $user );
}

/**
 * The name of the class a course (or a group, or a membership) stands for:
 * the end of its address, which is what a teacher types in the classes of a
 * live on the portal.
 *
 * @param int $post_id The course.
 * @return string
 */
function qqplay_class_name( $post_id ) {
	$slug = get_post_field( 'post_name', (int) $post_id );
	return is_string( $slug ) ? urldecode( $slug ) : '';
}

/**
 * The post types that are classes here, by the course plugins that are active.
 *
 * @return string[]
 */
function qqplay_class_post_types() {
	$types = array();
	if ( defined( 'LEARNDASH_VERSION' ) ) {
		$types[] = 'sfwd-courses';
		$types[] = 'groups';
	}
	if ( function_exists( 'tutor' ) ) {
		// Tutor answers through a magic property, which isset() does not see.
		try {
			$type = (string) tutor()->course_post_type;
		} catch ( \Throwable $error ) {
			$type = '';
		}
		$types[] = '' !== $type ? $type : 'courses';
	}
	if ( function_exists( 'llms_get_student' ) ) {
		$types[] = 'course';
		$types[] = 'llms_membership';
	}
	return (array) apply_filters( 'qqplay_class_post_types', array_values( array_unique( $types ) ) );
}

/**
 * The ids of the courses, groups and memberships someone is in.
 *
 * Each course plugin is asked in its own way, and a plugin that changed its
 * functions is skipped instead of breaking the page.
 *
 * @param WP_User $user Who.
 * @return int[]
 */
function qqplay_user_course_ids( WP_User $user ) {
	$ids = array();
	try {
		if ( function_exists( 'learndash_user_get_enrolled_courses' ) ) {
			$ids = array_merge( $ids, (array) learndash_user_get_enrolled_courses( $user->ID, array(), true ) );
		}
		if ( function_exists( 'learndash_get_users_group_ids' ) ) {
			$ids = array_merge( $ids, (array) learndash_get_users_group_ids( $user->ID ) );
		}
	} catch ( \Throwable $error ) {
		unset( $error );
	}
	try {
		if ( function_exists( 'tutor_utils' ) ) {
			$ids = array_merge( $ids, (array) tutor_utils()->get_enrolled_courses_ids_by_user( $user->ID ) );
		}
	} catch ( \Throwable $error ) {
		unset( $error );
	}
	try {
		$student = function_exists( 'llms_get_student' ) ? llms_get_student( $user->ID ) : false;
		if ( $student ) {
			foreach ( array( 'course', 'membership' ) as $kind ) {
				$found = $student->get_enrollments(
					$kind,
					array(
						'limit'  => 50,
						'status' => 'enrolled',
					)
				);
				if ( is_array( $found ) && ! empty( $found['results'] ) ) {
					$ids = array_merge( $ids, (array) $found['results'] );
				}
			}
		}
	} catch ( \Throwable $error ) {
		unset( $error );
	}
	return array_values( array_unique( array_filter( array_map( 'intval', $ids ) ) ) );
}

/**
 * Someone's classes: the courses they are enrolled in. Whoever moderates is
 * in every class ("*"), so the site's own staff is never kept out of a live.
 *
 * @param WP_User $user Who.
 * @return string[]
 */
function qqplay_user_classes( WP_User $user ) {
	$classes = array();
	if ( qqplay_user_is_teacher( $user ) ) {
		$classes[] = '*';
	} else {
		foreach ( qqplay_user_course_ids( $user ) as $id ) {
			$name = qqplay_class_name( $id );
			if ( '' !== $name && '*' !== $name ) {
				$classes[] = $name;
			}
		}
	}
	$classes = (array) apply_filters( 'qqplay_user_classes', array_values( array_unique( $classes ) ), $user );
	// The portal reads fifty classes of sixty characters at most.
	return array_slice( array_values( array_filter( array_map( 'strval', $classes ) ) ), 0, 50 );
}

/**
 * The token for someone signed in to this site.
 *
 * @param WP_User $user Who.
 * @return string
 */
function qqplay_user_token( WP_User $user ) {
	$now    = time();
	$name   = trim( (string) $user->display_name );
	$claims = array(
		'sub'  => (string) $user->ID,
		'name' => '' !== $name ? $name : (string) $user->user_login,
		'iat'  => $now,
		'exp'  => $now + 10 * MINUTE_IN_SECONDS,
	);
	if ( is_email( $user->user_email ) ) {
		$claims['email'] = $user->user_email;
	}
	// A picture the person really has: with no Gravatar the address answers
	// 404, and the portal keeps their initials instead of a grey silhouette.
	$picture = get_avatar_url(
		$user->ID,
		array(
			'size'    => 192,
			'default' => '404',
		)
	);
	if ( is_string( $picture ) && 0 === strpos( $picture, 'https://' ) ) {
		$claims['picture'] = $picture;
	}
	if ( qqplay_user_is_teacher( $user ) ) {
		$claims['moderator'] = true;
	}
	$classes = qqplay_user_classes( $user );
	if ( $classes ) {
		$claims['groups'] = $classes;
	}
	$claims = (array) apply_filters( 'qqplay_token_claims', $claims, $user );
	return qqplay_sign( $claims );
}

/**
 * The token this site's server talks to the portal's API with.
 *
 * @return string
 */
function qqplay_api_token() {
	$now = time();
	return qqplay_sign(
		array(
			'scope' => 'api',
			'iat'   => $now,
			'exp'   => $now + 5 * MINUTE_IN_SECONDS,
		)
	);
}
